#!/usr/bin/env python3
"""lumate research helper.

Fetches and caches PUBLIC research about the user, used to ground answers to
open-ended event questions ("what have you built?", "why attend?"). Two sources:

  - GitHub: public REST API (no auth), via urllib with a `gh` CLI fallback. The
    only network-touching part of lumate.
  - LinkedIn: this script does NOT scrape LinkedIn. It only stores a URL +
    headline that the user provided (Claude writes them via `linkedin-set`).

Everything here is disposable cache — the user's source of truth stays in
profile.json. Never mirror this file into long-term memory.

Cache location (in order):
  1. $LUMA_RESEARCH
  2. ~/.lumate/research.json

Usage:
  research.py path
  research.py show
  research.py github [<login>] [--max-age <hours>]   # fetch+cache GitHub
  research.py summary                                 # compact text for drafting
  research.py github-set [--json <file|->]            # inject hand-gathered data
  research.py linkedin-set --url <u> [--headline <h>] [--snippet <s> ...] [--confidence high|low]
  research.py linkedin-get
  research.py enrich-profile                           # propose profile fills (writes nothing)

GitHub fetch chain: urllib -> `gh api` (if installed) -> exit 1 with a reason.
`<login>` defaults to professional.github from profile.json.
`--max-age` skips the refetch when the cache is younger than N hours.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone

# Reuse profile.py's loader/dig so research can read the saved GitHub handle and
# propose enrichments. Same scripts/ dir, so make it importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import profile as pf  # noqa: E402

GITHUB_API = "https://api.github.com"
DEFAULT_MAX_AGE = 168.0  # hours (7 days), used by the SKILL flow's `--max-age 168`


class ResearchError(Exception):
    pass


# --------------------------------------------------------------------------- io

def research_path():
    p = os.environ.get("LUMA_RESEARCH")
    if p:
        return os.path.expanduser(p)
    return os.path.expanduser("~/.lumate/research.json")


def load_cache():
    path = research_path()
    if not os.path.exists(path):
        return {"_meta": {"schema": 1}, "github": {}, "linkedin": {}}
    with open(path, "r") as f:
        data = json.load(f)
    data.setdefault("_meta", {}).setdefault("schema", 1)
    data.setdefault("github", {})
    data.setdefault("linkedin", {})
    return data


def save_cache(data):
    path = research_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def age_hours(iso):
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return None


# ----------------------------------------------------------------- flag parsing

def parse_flags(args, repeatable=()):  # returns (flags, positionals)
    flags, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            has_val = i + 1 < len(args) and not args[i + 1].startswith("--")
            val = args[i + 1] if has_val else ""
            if key in repeatable:
                flags.setdefault(key, []).append(val)
            else:
                flags[key] = val
            i += 2 if has_val else 1
        else:
            pos.append(a)
            i += 1
    return flags, pos


# -------------------------------------------------------------------- github io

def _http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "lumate",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _gh_api(path):
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "api", path],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except Exception:
        return None


def fetch_github_raw(login):
    user = _http_get_json(f"{GITHUB_API}/users/{login}")
    if user is None or "login" not in user:
        user = _gh_api(f"users/{login}")
    if not user or "login" not in user:
        raise ResearchError(
            f"github: could not fetch user '{login}' "
            f"(404/private or urllib+gh both unavailable/rate-limited)")
    repos = _http_get_json(
        f"{GITHUB_API}/users/{login}/repos?per_page=100&sort=pushed")
    if not isinstance(repos, list):
        repos = _gh_api(f"users/{login}/repos?per_page=100&sort=pushed")
    if not isinstance(repos, list):
        repos = []
    return user, repos


def _trunc(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def build_github_block(user, repos):
    non_fork = [r for r in repos if not r.get("fork")]
    by_recent = sorted(non_fork, key=lambda r: r.get("pushed_at") or "", reverse=True)
    top = sorted(by_recent, key=lambda r: -(r.get("stargazers_count") or 0))[:8]
    top_repos = [{
        "name": r.get("name"),
        "desc": _trunc(r.get("description"), 120),
        "lang": r.get("language"),
        "stars": r.get("stargazers_count") or 0,
        "url": r.get("html_url"),
    } for r in top]

    notable = ", ".join(
        f"{r['name']} ({r['stars']}★{', ' + r['lang'] if r['lang'] else ''})"
        for r in top_repos[:5])
    langs = [r["lang"] for r in top_repos if r["lang"]]
    dom = ", ".join(l for l, _ in Counter(langs).most_common(3))
    bio = (user.get("bio") or "").strip()
    summary = ((bio + ". " if bio else "")
               + f"{user.get('public_repos', 0)} public repos."
               + (f" Notable: {notable}." if notable else "")
               + (f" Mainly {dom}." if dom else ""))

    return {
        "login": user.get("login"),
        "name": user.get("name"),
        "bio": bio,
        "blog": (user.get("blog") or "").strip(),
        "company": (user.get("company") or "").lstrip("@").strip() or None,
        "location": user.get("location"),
        "twitter_username": user.get("twitter_username"),
        "public_repos": user.get("public_repos", 0),
        "followers": user.get("followers", 0),
        "top_repos": top_repos,
        "summary": summary,
    }


# ------------------------------------------------------------------ subcommands

def cmd_path():
    print(research_path())


def cmd_show():
    print(json.dumps(load_cache(), indent=2, ensure_ascii=False))


def cmd_github(rest):
    flags, pos = parse_flags(rest)
    login = pos[0] if pos else pf.dig(pf.load(), "professional.github")
    if not login:
        print("github: no login (pass <login> or set professional.github)",
              file=sys.stderr)
        return 1
    cache = load_cache()
    if "max-age" in flags:
        try:
            max_age = float(flags["max-age"])
        except ValueError:
            max_age = DEFAULT_MAX_AGE
        fetched = cache["_meta"].get("github_fetched_at")
        a = age_hours(fetched) if fetched else None
        if a is not None and a < max_age:
            print(f"fresh: github cached {round(a, 1)}h ago (< {max_age}h); skipping fetch")
            return 0
    try:
        user, repos = fetch_github_raw(login)
    except ResearchError as e:
        print(str(e), file=sys.stderr)
        return 1
    cache["github"] = build_github_block(user, repos)
    cache["_meta"]["github_fetched_at"] = now_iso()
    save_cache(cache)
    print(f"github: cached {len(cache['github']['top_repos'])} top repos for "
          f"{cache['github'].get('login')}")
    return 0


def cmd_github_set(rest):
    flags, _ = parse_flags(rest)
    src = flags.get("json", "-")
    raw = sys.stdin.read() if src in ("", "-") else open(os.path.expanduser(src)).read()
    try:
        payload = json.loads(raw)
    except Exception as e:
        print(f"github-set: invalid JSON ({e})", file=sys.stderr)
        return 1
    if "user" in payload:
        block = build_github_block(payload["user"], payload.get("repos", []))
    elif "login" in payload and "top_repos" not in payload:
        block = build_github_block(payload, payload.get("repos", []))
    else:
        block = payload  # already a built github block
    cache = load_cache()
    cache["github"] = block
    cache["_meta"]["github_fetched_at"] = now_iso()
    save_cache(cache)
    print(f"github-set: stored block for {block.get('login')}")
    return 0


def cmd_summary():
    cache = load_cache()
    gh = cache.get("github", {})
    li = cache.get("linkedin", {})
    parts = []
    if gh.get("summary"):
        parts.append("GitHub: " + gh["summary"])
    if li.get("headline"):
        conf = li.get("confidence")
        parts.append(f"LinkedIn: {li['headline']}" + (f" ({conf} confidence)" if conf else ""))
    print("\n".join(parts) if parts else "(no research cached yet)")


def cmd_linkedin_set(rest):
    flags, _ = parse_flags(rest, repeatable=("snippet",))
    url = flags.get("url", "").strip()
    if not url:
        print("linkedin-set: --url is required", file=sys.stderr)
        return 1
    cache = load_cache()
    cache["linkedin"] = {
        "url": url,
        "headline": flags.get("headline", "").strip(),
        "snippets": flags.get("snippet", []),
        "confidence": flags.get("confidence", "").strip(),
        "source": "user",
    }
    cache["_meta"]["linkedin_fetched_at"] = now_iso()
    save_cache(cache)
    print(f"linkedin-set: stored {url}")
    return 0


def cmd_linkedin_get():
    print(json.dumps(load_cache().get("linkedin", {}), indent=2, ensure_ascii=False))


def cmd_enrich_profile():
    """Print proposed values for EMPTY profile fields, derived from research.
    Writes nothing — Claude shows these in onboarding and persists accepted ones
    via `profile.py set`. Output: key<TAB>value<TAB>source (one per line)."""
    cache = load_cache()
    gh = cache.get("github", {})
    li = cache.get("linkedin", {})
    prof = pf.load()
    rows = []

    def propose(key, value, source):
        value = (value or "")
        if isinstance(value, str):
            value = value.strip()
        if value and not (pf.dig(prof, key) or "").strip():
            rows.append((key, value, source))

    website = gh.get("blog") or ""
    if website and not website.startswith("http"):
        website = "https://" + website
    twitter = gh.get("twitter_username")

    propose("identity.full_name", gh.get("name"), "github")
    propose("professional.website", website, "github")
    propose("professional.twitter", ("@" + twitter) if twitter else "", "github")
    propose("professional.company", gh.get("company"), "github")
    propose("professional.city", gh.get("location"), "github")
    propose("about.bio", gh.get("bio"), "github")
    propose("about.headline", li.get("headline") or gh.get("bio"),
            "linkedin" if li.get("headline") else "github")
    langs = [r.get("lang") for r in gh.get("top_repos", []) if r.get("lang")]
    if langs:
        propose("about.skills", ", ".join(l for l, _ in Counter(langs).most_common(4)),
                "github:inferred")

    for key, value, source in rows:
        print(f"{key}\t{value}\t{source}")


# ------------------------------------------------------------------------- main

def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "path":
        return cmd_path() or 0
    if cmd == "show":
        return cmd_show() or 0
    if cmd == "github":
        return cmd_github(rest)
    if cmd == "github-set":
        return cmd_github_set(rest)
    if cmd == "summary":
        return cmd_summary() or 0
    if cmd == "linkedin-set":
        return cmd_linkedin_set(rest)
    if cmd == "linkedin-get":
        return cmd_linkedin_get() or 0
    if cmd == "enrich-profile":
        return cmd_enrich_profile() or 0
    print(f"unknown command: {cmd}\n", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
