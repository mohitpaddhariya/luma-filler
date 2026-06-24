#!/usr/bin/env python3
"""lumate browser helper — self-contained headless Playwright.

Replaces the gstack `browse` binary. No daemon: each command launches a
persistent browser context, navigates, acts, and exits. Login persists across
runs in a dedicated profile dir, so the read -> ask -> fill -> confirm -> submit
flow works across separate invocations (each re-navigates and re-applies state).

Requires only the Playwright Python client, installed in a dedicated venv (the
system Python is externally managed — PEP 668):
    python3 -m venv ~/.lumate/venv
    ~/.lumate/venv/bin/pip install playwright
The Chromium binary is NOT downloaded — we launch the build already cached on disk
via executable_path (the shared ms-playwright cache). This script self-bootstraps
into that venv, so you can invoke it with the system `python3`. If the cache is
ever gone, run `~/.lumate/venv/bin/playwright install chromium` once.

NOTE: headless Chromium is killed by Claude Code's default Bash sandbox, so run
these commands either with the Bash sandbox disabled or via the `!` shell-prefix.

Profile (persistent login):  ~/.lumate/pw-profile/
Screenshots default to       ~/.lumate/

Commands:
  browser.py login                       # headed one-time sign-in (persists)
  browser.py status                      # report logged-in state (JSON)
  browser.py read  <url>                 # event text + form fields (JSON) + screenshot
  browser.py fill  <url> <answers.json>  # apply answers (no submit) + screenshot
  browser.py submit <url> <answers.json> # apply answers, click submit, report result
  browser.py apply <url> <answers.json> [--submit]   # one-shot read+fill(+submit); returns missing_required[]
  browser.py cache-get <url>             # cached field list for a known event (no launch)
  browser.py screenshot <url> [path]

answers.json = a list of:
  {"match": {"name"|"label"|"placeholder"|"text": "..."}, "action": "fill"|"type"|"select"|"click", "value": "..."}
"""

import glob
import json
import os
import re
import sys
import time

PROFILE_DIR = os.path.expanduser("~/.lumate/pw-profile")
SHOT_DIR = os.path.expanduser("~/.lumate")
VENV_PY = os.path.expanduser("~/.lumate/venv/bin/python")

# Present as a normal desktop Chrome so a legit (logged-in) session isn't
# false-flagged as a headless bot. This is fingerprint hygiene, NOT a captcha
# bypass — if a human-check is actually shown, the skill still hands off.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

PROCEED = re.compile(r"(register|rsvp|request to join|join (the )?event|one.?click|i'?m going|attend|going)", re.I)
PAID = re.compile(r"(get tickets?|checkout|buy|purchase|\$\s?\d|reserve.*\$)", re.I)
CLOSED = re.compile(r"(registration closed|sold out|join waitlist|waitlist|event ended|at capacity|\bfull\b)", re.I)
SUBMIT = re.compile(r"(register|request to join|rsvp|submit|confirm|complete|get tickets?|reserve|continue|one.?click|going)", re.I)
SUCCESS = re.compile(r"(you'?re in|you are in|registered|request (received|sent)|pending approval|you'?re going|see you|added to calendar|your ticket|qr code)", re.I)
CAPTCHA = re.compile(r"(verify you are human|verifying your browser|are you human|cloudflare|hcaptcha|recaptcha|complete the captcha)", re.I)

COMBO_PLACEHOLDER = "select an option"   # Luma's lazy custom-dropdown trigger text
COMBO_PROBE_BUDGET_S = 8.0
COMBO_OPEN_WAIT_MS = 700
COMBO_MAX_OPTIONS = 40
FORMS_CACHE = os.path.expanduser("~/.lumate/forms-cache.json")

# Visible leaf-text snapshot — diffing it before/after opening a combo reveals the
# option list (Luma renders options as non-ARIA divs, so role=option finds nothing).
JS_VISIBLE_LEAVES = r"""
() => { const out=[];
  document.querySelectorAll('div,li,button,span,p,a,option').forEach(e=>{
    const r=e.getBoundingClientRect(); const t=(e.innerText||'').trim();
    if(r.width>0 && r.height>0 && t && t.length<45 && e.children.length===0) out.push(t);
  }); return out; }
"""

# Shared JS: best-effort accessible label for a control, cleaned of the nbsp +
# required-asterisk noise Luma adds ("Your Country *" -> "Your Country").
JS_LABEL_FN = r"""
  const clean = (s) => (s || '').replace(/ /g, ' ').replace(/\s*\*+\s*$/, '').replace(/\s+/g, ' ').trim();
  const labelFor = (e) => {
    if (e.id) { try { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l && l.innerText.trim()) return clean(l.innerText); } catch (x) {} }
    const wrap = e.closest('label'); if (wrap && wrap.innerText.trim()) return clean(wrap.innerText);
    const al = e.getAttribute('aria-label'); if (al) return clean(al);
    const lb = e.getAttribute('aria-labelledby'); if (lb) { const n = document.getElementById(lb); if (n && n.innerText.trim()) return clean(n.innerText); }
    let node = e;
    for (let up = 0; up < 4 && node.parentElement; up++) {
      node = node.parentElement;
      const lab = node.querySelector('label, .label, [class*="label"], [class*="Label"]');
      if (lab && lab.innerText.trim() && lab.contains(e) === false) return clean(lab.innerText.split('\n')[0]);
    }
    return '';
  };
  const ctrlsIn = (root) => Array.from(root.querySelectorAll('input, textarea, select'))
    .filter(e => e.type !== 'hidden' && e.offsetParent !== null);
  const norm = (s) => clean(s).toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
"""

# One DOM walk that returns every standard control with its best-effort label.
JS_EXTRACT_FIELDS = r"""
() => {
""" + JS_LABEL_FN + r"""
  const within = document.querySelector('form') || document.querySelector('[role=dialog]') || document.body;
  const ctrls = ctrlsIn(within);
  return ctrls.map((e, i) => {
    const tag = e.tagName.toLowerCase();
    const type = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (e.getAttribute('type') || 'text');
    let options = [];
    if (tag === 'select') options = Array.from(e.options).map(o => (o.text || '').trim()).filter(Boolean);
    const ml = e.getAttribute('maxlength');
    return {
      idx: i, tag, type,
      label: labelFor(e),
      name: e.getAttribute('name') || '',
      placeholder: e.getAttribute('placeholder') || '',
      maxlength: ml ? parseInt(ml, 10) : null,
      required: e.hasAttribute('required') || e.getAttribute('aria-required') === 'true',
      options,
    };
  });
}
"""

# Tag the control whose normalized label matches `target`, so Python can locate it
# even when the field has no name attribute (Luma's custom-question textareas).
JS_FIND_BY_LABEL = r"""
(target) => {
""" + JS_LABEL_FN + r"""
  const within = document.querySelector('form') || document.querySelector('[role=dialog]') || document.body;
  document.querySelectorAll('[data-luma-target]').forEach(e => e.removeAttribute('data-luma-target'));
  let exact = null, partial = null;
  for (const e of ctrlsIn(within)) {
    const nl = norm(labelFor(e));
    if (!nl) continue;
    if (nl === target) { exact = e; break; }
    if (!partial && (nl.includes(target) || target.includes(nl))) partial = e;
  }
  const hit = exact || partial;
  if (hit) { hit.setAttribute('data-luma-target', '1'); return true; }
  return false;
}
"""


def _norm(s):
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s*\*+\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pw():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        # System python lacks playwright (PEP 668). Re-exec into the skill's venv.
        # Use sys.prefix (not realpath of the exe — a venv python symlinks to the
        # base interpreter, so realpath would collapse them and we'd never switch).
        in_venv = os.path.abspath(sys.prefix) == os.path.abspath(os.path.dirname(os.path.dirname(VENV_PY)))
        if not in_venv and os.path.exists(VENV_PY):
            os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])
        sys.stderr.write(
            "playwright not available. One-time setup:\n"
            "  python3 -m venv ~/.lumate/venv\n"
            "  ~/.lumate/venv/bin/pip install playwright\n")
        sys.exit(3)


def find_chromium():
    bases = [os.path.expanduser("~/Library/Caches/ms-playwright"),
             os.path.expanduser("~/.cache/ms-playwright")]
    pats = [
        "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-win/chrome.exe",
    ]
    cands = []
    for b in bases:
        for pat in pats:
            cands += glob.glob(os.path.join(b, pat))

    def build(path):
        m = re.search(r"chromium-(\d+)", path)
        return int(m.group(1)) if m else 0
    cands.sort(key=build, reverse=True)
    return cands[0] if cands else None


def normalize_url(u):
    u = (u or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("lu.ma") or u.startswith("www.lu.ma") or u.startswith("luma.com"):
        return "https://" + u
    if "/" not in u and "." not in u:        # bare slug
        return "https://lu.ma/" + u
    return "https://" + u


def launch(sp, headless=True):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    exe = find_chromium()
    kwargs = dict(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 960},
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="Asia/Kolkata",
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    if exe:
        kwargs["executable_path"] = exe
    ctx = sp.chromium.launch_persistent_context(**kwargs)
    # Drop the navigator.webdriver automation tell so a real logged-in session
    # reads as an ordinary browser. (Not a challenge solver.)
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(20000)
    return ctx, page


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def is_logged_in(ctx, page):
    try:
        for c in ctx.cookies():
            dom = c.get("domain") or ""
            # auth token is `luma.auth-session-key` on `.luma.com` (not `lu.ma`)
            if ("lu.ma" in dom or "luma.com" in dom) and re.search(r"auth|session", c.get("name", ""), re.I):
                return True
    except Exception:
        pass
    try:
        if page.get_by_role("button", name=re.compile(r"sign in|log ?in", re.I)).count() == 0 \
           and page.get_by_text(re.compile(r"sign in|log ?in", re.I)).count() == 0:
            return True
    except Exception:
        pass
    return False


def find_primary_button(page):
    """Return (locator, text, kind) for the page's main CTA, kind in
    proceed|paid|closed, or (None, '', None)."""
    found = {}
    try:
        els = page.get_by_role("button").all() + page.locator("a[role=button]").all()
    except Exception:
        els = []
    for el in els:
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
        except Exception:
            continue
        if not t:
            continue
        kind = "paid" if PAID.search(t) else "closed" if CLOSED.search(t) else "proceed" if PROCEED.search(t) else None
        if kind and kind not in found:
            found[kind] = (el, t, kind)
    for kind in ("proceed", "paid", "closed"):
        if kind in found:
            return found[kind]
    return (None, "", None)


def settle_form(page, ms=3000):
    """Wait for Luma's lazily-rendered widgets (custom dropdowns) to appear."""
    page.wait_for_timeout(ms)


def _visible_leaves(page):
    try:
        return page.evaluate(JS_VISIBLE_LEAVES) or []
    except Exception:
        return []


def _probe_combo_options(page, label):
    """Open a 'Select an option' combo and capture its options by diffing the
    visible leaf text before vs after the click (options are non-ARIA divs)."""
    try:
        if not page.evaluate(JS_FIND_BY_LABEL, _norm(label)):
            return []
        ctrl = page.locator('[data-luma-target="1"]').first
        before = set(_visible_leaves(page))
        ctrl.scroll_into_view_if_needed()
        ctrl.click(timeout=4000)
        page.wait_for_timeout(COMBO_OPEN_WAIT_MS)
        opts = [t for t in dict.fromkeys(_visible_leaves(page)) if t not in before]
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)   # let the popup fully close before the next combo is baselined
        except Exception:
            pass
        return opts[:COMBO_MAX_OPTIONS]
    except Exception:
        return []


def extract_fields(page, probe_combos=True):
    try:
        fields = page.evaluate(JS_EXTRACT_FIELDS) or []
    except Exception:
        return []
    if not probe_combos:
        return fields
    # Luma's lazy "Select an option" comboboxes come through as plain text inputs.
    # Upgrade each to type="combo" with its real option list + required=True so the
    # skill can pre-match a saved answer to an exact option with no live probing.
    combos = [f for f in fields
              if (f.get("placeholder") or "").strip().lower() == COMBO_PLACEHOLDER]
    deadline = time.time() + COMBO_PROBE_BUDGET_S
    for f in combos:
        f["type"] = "combo"
        f["required"] = True
        if time.time() > deadline:
            f["options"] = []
            f["probe"] = "skipped:budget"
            continue
        f["options"] = _probe_combo_options(page, f.get("label") or "")
    return fields


def robust_click(el, page):
    """Click that survives overlay / pointer-event interception (logged-in Luma
    layouts stack cards over the CTA). Tries a direct JS dispatch (which ignores
    anything painted on top) before a forced coordinate click — a force click can
    silently land on the overlay instead of the button."""
    for how in ("normal", "js", "force"):
        try:
            if how == "normal":
                el.click(timeout=6000)
            elif how == "js":
                el.evaluate("e => e.click()")
            else:
                el.click(force=True, timeout=6000)
            return True
        except Exception:
            continue
    return False


def open_form(page):
    """Click the primary CTA to reveal the form. Verifies the form actually opened
    after each click strategy (a click that doesn't throw can still hit an overlay).
    Returns (button_text, kind, opened)."""
    el, text, kind = find_primary_button(page)
    if el is None or kind != "proceed":
        return text, kind, False
    for how in ("js", "normal", "force"):
        try:
            if how == "js":
                el.evaluate("e => e.click()")
            elif how == "normal":
                el.click(timeout=5000)
            else:
                el.click(force=True, timeout=5000)
        except Exception:
            continue
        page.wait_for_timeout(1500)
        try:
            page.wait_for_selector("textarea, input:not([type=hidden]), select, [role=dialog]", timeout=6000)
        except Exception:
            pass
        if page.locator("[role=dialog]").count() or len(extract_fields(page, probe_combos=False)):
            return text, kind, True
    return text, kind, False


def locate(page, match):
    name = match.get("name")
    label = match.get("label")
    ph = match.get("placeholder")
    text = match.get("text")
    try:
        if name:
            loc = page.locator(f'[name="{name}"]')
            if loc.count():
                return loc.first
        if label:
            # Normalized-label tag first — robust to nbsp and no-name textareas.
            try:
                if page.evaluate(JS_FIND_BY_LABEL, _norm(label)):
                    loc = page.locator('[data-luma-target="1"]')
                    if loc.count():
                        return loc.first
            except Exception:
                pass
            try:
                loc = page.get_by_label(re.compile(re.escape(label), re.I))
                if loc.count():
                    return loc.first
            except Exception:
                pass
        if ph:
            loc = page.get_by_placeholder(re.compile(re.escape(ph), re.I))
            if loc.count():
                return loc.first
        if text:
            loc = page.get_by_text(re.compile(re.escape(text), re.I))
            if loc.count():
                return loc.first
    except Exception:
        return None
    return None


def apply_answers(page, answers):
    applied, failed = [], []
    if any(a.get("action") == "combo" for a in answers):
        page.wait_for_timeout(2500)   # Luma's custom dropdowns render lazily
    for a in answers:
        match = a.get("match", {})
        action = a.get("action", "fill")
        value = a.get("value", "")
        tag = match.get("name") or match.get("label") or match.get("placeholder") or match.get("text") or "?"
        loc = locate(page, match)
        if loc is None:
            failed.append({"field": tag, "reason": "not found"})
            continue
        try:
            if action == "fill":
                loc.fill(value)
            elif action == "type":
                loc.click()
                loc.type(value, delay=15)
            elif action == "select":
                try:
                    loc.select_option(label=value)
                except Exception:
                    loc.select_option(value)
            elif action == "click":
                loc.click()
            elif action == "combo":
                # Luma lazy custom dropdown: click the labelled input to open it,
                # then click the visible option whose text == value.
                loc.click()
                page.wait_for_timeout(700)
                picked = False
                cand = page.get_by_text(value, exact=True)
                for i in range(min(cand.count(), 12)):
                    c = cand.nth(i)
                    try:
                        if c.is_visible():
                            c.click()
                            picked = True
                            break
                    except Exception:
                        continue
                if not picked:
                    raise RuntimeError(f"option '{value}' not visible")
                page.wait_for_timeout(300)
            else:
                failed.append({"field": tag, "reason": f"unknown action {action}"})
                continue
            applied.append({"field": tag, "action": action})
        except Exception as e:
            failed.append({"field": tag, "reason": str(e)[:120]})
    return applied, failed


def find_submit(page):
    scopes = []
    try:
        if page.locator("[role=dialog]").count():
            scopes.append(page.locator("[role=dialog]"))
        if page.locator("form").count():
            scopes.append(page.locator("form"))
    except Exception:
        pass
    scopes.append(page)
    for scope in scopes:
        try:
            btns = scope.get_by_role("button").all()
        except Exception:
            btns = []
        match = None
        for el in btns:
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or "").strip()
            except Exception:
                continue
            if t and SUBMIT.search(t) and not CLOSED.search(t):
                match = (el, t)        # keep last visible match (CTA usually at bottom)
        if match:
            return match
    return (None, "")


# ------------------------------------------------------------------ subcommands

def cmd_login():
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=False)
        try:
            goto(page, "https://lu.ma/signin")
            sys.stderr.write("A Chrome window opened. Sign in to Luma; this waits up to 5 min...\n")
            ok = False
            for _ in range(60):
                page.wait_for_timeout(5000)
                if is_logged_in(ctx, page):
                    ok = True
                    break
            print(json.dumps({"logged_in": ok}))
        finally:
            ctx.close()


def cmd_status():
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=True)
        try:
            goto(page, "https://lu.ma/home")
            print(json.dumps({"logged_in": is_logged_in(ctx, page)}))
        finally:
            ctx.close()


def cmd_read(url):
    url = normalize_url(url)
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=True)
        try:
            goto(page, url)
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            event_text = ""
            try:
                event_text = (page.inner_text("body") or "")[:6000]
            except Exception:
                pass
            button_text, kind, clicked = open_form(page)
            if clicked:
                settle_form(page)
            fields = extract_fields(page) if clicked else []
            instant = False
            if clicked and not fields:
                body = (page.inner_text("body") or "")
                instant = bool(SUCCESS.search(body))
            shot = os.path.join(SHOT_DIR, "last-form.png")
            os.makedirs(SHOT_DIR, exist_ok=True)
            try:
                page.screenshot(path=shot, full_page=False)
            except Exception:
                shot = ""
            print(json.dumps({
                "url": url,
                "title": title,
                "button_label": button_text,
                "button_kind": kind,           # proceed | paid | closed | None
                "form_opened": clicked,
                "instant_registered": instant,
                "fields": fields,
                "event_text": event_text,
                "screenshot": shot,
            }, ensure_ascii=False))
        finally:
            ctx.close()


def _load_answers(path):
    with open(os.path.expanduser(path)) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("answers.json must be a JSON list")
    return data


def cmd_fill(url, answers_path, do_submit=False):
    url = normalize_url(url)
    answers = _load_answers(answers_path)
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=True)
        try:
            goto(page, url)
            button_text, kind, clicked = open_form(page)
            applied, failed = apply_answers(page, answers)
            os.makedirs(SHOT_DIR, exist_ok=True)
            out = {"url": url, "form_opened": clicked, "applied": applied, "failed": failed}
            if not do_submit:
                shot = os.path.join(SHOT_DIR, "filled.png")
                try:
                    page.screenshot(path=shot)
                except Exception:
                    shot = ""
                out["screenshot"] = shot
                print(json.dumps(out, ensure_ascii=False))
                return
            # submit
            btn, btn_text = find_submit(page)
            if btn is None:
                out["submitted"] = False
                out["reason"] = "submit button not found"
                print(json.dumps(out, ensure_ascii=False))
                return
            if not robust_click(btn, page):
                out["submitted"] = False
                out["reason"] = "could not click submit button"
                print(json.dumps(out, ensure_ascii=False))
                return
            page.wait_for_timeout(3000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            body = (page.inner_text("body") or "")
            shot = os.path.join(SHOT_DIR, "result.png")
            try:
                page.screenshot(path=shot, full_page=True)
            except Exception:
                shot = ""
            out.update({
                "submitted": True,
                "submit_button": btn_text,
                "success": bool(SUCCESS.search(body)),
                "captcha": bool(CAPTCHA.search(body)),
                "result_text": body[:1500],
                "screenshot": shot,
            })
            print(json.dumps(out, ensure_ascii=False))
        finally:
            ctx.close()


def cmd_screenshot(url, path=None):
    url = normalize_url(url)
    path = os.path.expanduser(path) if path else os.path.join(SHOT_DIR, "shot.png")
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=True)
        try:
            goto(page, url)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            page.screenshot(path=path, full_page=True)
            print(json.dumps({"screenshot": path}))
        finally:
            ctx.close()


# ----------------------------------------------------------------- forms cache

def _cache_key(url):
    return re.sub(r"[?#].*$", "", normalize_url(url))


def cache_load():
    try:
        with open(FORMS_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def cache_put(url, fields):
    try:
        c = cache_load()
        c[_cache_key(url)] = {"fields": fields}
        tmp = FORMS_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(tmp, FORMS_CACHE)
    except Exception:
        pass


def cmd_cache_get(url):
    print(json.dumps(cache_load().get(_cache_key(url), {}), ensure_ascii=False))


def _field_covered(field, answers):
    """Is this form field targeted by some entry in answers.json?"""
    name = (field.get("name") or "").strip()
    nlabel = _norm(field.get("label") or "")
    for a in answers:
        m = a.get("match", {})
        if name and m.get("name") == name:
            return True
        ml = _norm(m.get("label") or "")
        if nlabel and ml and (ml == nlabel or ml in nlabel or nlabel in ml):
            return True
    return False


def cmd_apply(url, answers_path, do_submit=False):
    """One-shot: navigate + open form + fill + (optionally) submit in ONE browser
    session. Returns discovered fields[] and missing_required[] (required fields the
    answers did not cover) so the skill can fall back to asking. Refuses to submit
    while any required field is uncovered or failed to fill."""
    url = normalize_url(url)
    answers = _load_answers(answers_path)
    sp_fn = _pw()
    with sp_fn() as sp:
        ctx, page = launch(sp, headless=True)
        try:
            goto(page, url)
            button_text, kind, clicked = open_form(page)
            out = {"url": url, "button_label": button_text, "button_kind": kind,
                   "form_opened": clicked}
            if kind in ("paid", "closed"):
                out["halted"] = kind
                print(json.dumps(out, ensure_ascii=False))
                return
            if clicked:
                settle_form(page)
            fields = extract_fields(page) if clicked else []
            out["fields"] = fields
            if clicked and not fields:
                body = (page.inner_text("body") or "")
                if SUCCESS.search(body):
                    out["instant_registered"] = True
                    out["success"] = True
                    print(json.dumps(out, ensure_ascii=False))
                    return
            cache_put(url, fields)
            missing = [(f.get("label") or f.get("name") or "?")
                       for f in fields if f.get("required") and not _field_covered(f, answers)]
            out["missing_required"] = missing
            applied, failed = apply_answers(page, answers)
            out["applied"] = applied
            out["failed"] = failed
            os.makedirs(SHOT_DIR, exist_ok=True)
            if not do_submit:
                shot = os.path.join(SHOT_DIR, "filled.png")
                try:
                    page.screenshot(path=shot)
                except Exception:
                    shot = ""
                out["screenshot"] = shot
                print(json.dumps(out, ensure_ascii=False))
                return
            # Submit guard: never submit while a REQUIRED field is uncovered or failed.
            req_norm = {_norm(f.get("label") or "") for f in fields if f.get("required")}
            failed_req = [x.get("field") for x in failed if _norm(str(x.get("field"))) in req_norm]
            if missing or failed_req:
                out["submitted"] = False
                out["reason"] = "required not covered/filled: " + ", ".join(missing + failed_req)
                try:
                    page.screenshot(path=os.path.join(SHOT_DIR, "filled.png"))
                except Exception:
                    pass
                print(json.dumps(out, ensure_ascii=False))
                return
            btn, btn_text = find_submit(page)
            if btn is None:
                out["submitted"] = False
                out["reason"] = "submit button not found"
                print(json.dumps(out, ensure_ascii=False))
                return
            if not robust_click(btn, page):
                out["submitted"] = False
                out["reason"] = "could not click submit button"
                print(json.dumps(out, ensure_ascii=False))
                return
            page.wait_for_timeout(3000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            body = (page.inner_text("body") or "")
            shot = os.path.join(SHOT_DIR, "result.png")
            try:
                page.screenshot(path=shot, full_page=True)
            except Exception:
                shot = ""
            out.update({
                "submitted": True,
                "submit_button": btn_text,
                "success": bool(SUCCESS.search(body)),
                "captcha": bool(CAPTCHA.search(body)),
                "result_text": body[:1500],
                "screenshot": shot,
            })
            print(json.dumps(out, ensure_ascii=False))
        finally:
            ctx.close()


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "login":
        return cmd_login() or 0
    if cmd == "status":
        return cmd_status() or 0
    if cmd == "read":
        if not rest:
            sys.stderr.write("read needs <url>\n")
            return 2
        return cmd_read(rest[0]) or 0
    if cmd == "fill":
        if len(rest) < 2:
            sys.stderr.write("fill needs <url> <answers.json>\n")
            return 2
        return cmd_fill(rest[0], rest[1], do_submit=False) or 0
    if cmd == "submit":
        if len(rest) < 2:
            sys.stderr.write("submit needs <url> <answers.json>\n")
            return 2
        return cmd_fill(rest[0], rest[1], do_submit=True) or 0
    if cmd == "screenshot":
        if not rest:
            sys.stderr.write("screenshot needs <url> [path]\n")
            return 2
        return cmd_screenshot(rest[0], rest[1] if len(rest) > 1 else None) or 0
    if cmd == "apply":
        if len(rest) < 2:
            sys.stderr.write("apply needs <url> <answers.json> [--submit]\n")
            return 2
        return cmd_apply(rest[0], rest[1], do_submit=("--submit" in rest)) or 0
    if cmd == "cache-get":
        if not rest:
            sys.stderr.write("cache-get needs <url>\n")
            return 2
        return cmd_cache_get(rest[0]) or 0
    sys.stderr.write(f"unknown command: {cmd}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
