#!/usr/bin/env python3
"""luma-filler profile helper.

A tiny, dependency-free CLI for reading/writing the saved Luma registration
profile and fuzzy-matching a custom event question to a previously stored answer.

Profile location (in order):
  1. $LUMA_PROFILE
  2. ~/.luma-filler/profile.json

Usage:
  profile.py init                      # create profile.json from template if missing
  profile.py show                      # print the whole profile (pretty JSON)
  profile.py path                      # print the resolved profile path
  profile.py get <dotted.key>          # print one value ("" if unset)
  profile.py set <dotted.key> <value>  # set a standard field
  profile.py missing                   # list empty standard identity fields (one per line)
  profile.py match "<question>"        # fuzzy-find a stored custom answer -> JSON
  profile.py remember "<question>" "<answer>"   # store a custom Q->A pair
  profile.py list-custom               # print all stored custom Q->A pairs
  profile.py route "<field label>"     # deterministic label->key route -> JSON {key,value,known}
  profile.py persist-field "<label>" "<value>"   # store any answer in its routed key (else custom_answers)
  profile.py forget "<question>"       # delete a stored custom answer

`match` prints JSON: {"matched": bool, "question": "<stored q>", "value": "<answer>", "score": 0.0-1.0}
A match is only reported when score >= threshold (default 0.62, override with $LUMA_MATCH_THRESHOLD).
"""

import json
import os
import re
import sys
from difflib import SequenceMatcher

TEMPLATE = {
    "identity": {
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "email": "",
        "phone": "",
    },
    "professional": {
        "company": "",
        "work_email": "",
        "job_title": "",
        "role": "",
        "linkedin": "",
        "twitter": "",
        "github": "",
        "website": "",
        "city": "",
        "country": "",
        "product_role": "",       # "Are you in a product role?" style yes/no
        "community": "",          # "join our community?" style yes/no
        "experience_level": "",   # seniority / years
    },
    "preferences": {
        "dietary": "",
        "tshirt_size": "",
        "pronouns": "",
        "languages": "",
        "marketing_opt_in": "No",   # default-decline newsletters so the box never re-asks
    },
    # Narrative self-description used to ground answers to open-ended event
    # questions ("what have you built?", "why attend?"). Hand-authored / confirmed
    # during onboarding; never invented. Empty fields are simply omitted from drafts.
    "about": {
        "headline": "",        # one-line professional identity (e.g. LinkedIn headline)
        "bio": "",             # 1-3 sentence self-description
        "what_i_build": "",    # focus area / what they make
        "current_focus": "",   # what they're working on now
        "interests": "",       # domains / topics they care about
        "goals": "",           # default "why I attend events" angle
        "skills": "",          # comma-separated stack
        "fun_fact": "",        # optional icebreaker
    },
    # Free-form question -> answer memory. Keyed by the *normalized* question so
    # slightly different phrasings of the same question still match.
    "custom_answers": {},
    # Deterministic label -> profile-key routing for the finite set of fields Luma
    # forms ask repeatedly. A label matches a keyword-set when ALL its words appear
    # in the normalized label; the most-specific (longest) matching set wins. This
    # takes the recurring fields out of fuzzy matching entirely, so they never
    # auto-fill wrong or get re-asked.
    "field_aliases": {
        "identity.full_name": [["full", "name"], ["name"]],
        "identity.email": [["email"]],
        "identity.phone": [["mobile", "number"], ["phone"], ["mobile"], ["contact", "number"], ["whatsapp"]],
        "professional.work_email": [["work", "email"], ["company", "email"], ["official", "email"]],
        "professional.company": [["company"], ["organization"], ["organisation"], ["employer"], ["startup", "name"]],
        "professional.job_title": [["current", "job"], ["job", "title"], ["title"], ["role"], ["designation"], ["position"], ["occupation"]],
        "professional.linkedin": [["linkedin"]],
        "professional.twitter": [["twitter"], ["x", "handle"]],
        "professional.github": [["github"]],
        "professional.website": [["website"], ["portfolio"], ["personal", "site"]],
        "professional.city": [["city"]],
        "professional.country": [["country"]],
        "professional.product_role": [["product", "role"], ["currently", "product"], ["work", "product"]],
        "professional.community": [["join", "community"], ["private", "community"], ["professionals", "community"], ["community"]],
        "professional.experience_level": [["experience", "level"], ["years", "experience"], ["seniority"]],
        "preferences.languages": [["comfortable", "languages"], ["languages"], ["language"]],
        "preferences.dietary": [["dietary"], ["diet"], ["food", "preference"], ["allergies"]],
        "preferences.tshirt_size": [["shirt", "size"], ["tshirt"], ["t", "shirt"]],
        "preferences.pronouns": [["pronouns"]],
        "preferences.marketing_opt_in": [["happy", "hear"], ["hear", "from"], ["updates"], ["newsletter"], ["marketing"], ["subscribe"]],
    },
}

# Standard identity/professional fields most Luma forms ask for, in priority order.
STANDARD_REQUIRED = ["identity.full_name", "identity.email"]


def profile_path():
    p = os.environ.get("LUMA_PROFILE")
    if p:
        return os.path.expanduser(p)
    return os.path.expanduser("~/.luma-filler/profile.json")


def load():
    path = profile_path()
    if not os.path.exists(path):
        return json.loads(json.dumps(TEMPLATE))  # deep copy
    with open(path, "r") as f:
        data = json.load(f)
    # Backfill any missing top-level sections so older profiles keep working.
    for k, v in TEMPLATE.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict):
            for sk, sv in v.items():
                data[k].setdefault(sk, sv)
    return data


def save(data):
    path = profile_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def dig(data, dotted):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def put(data, dotted, value):
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _norm_label(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def route_label(data, label):
    """Deterministically map a form-field label to a profile key via field_aliases.
    Returns (key, value) — value is the current stored value ("" if unset) — or
    (None, None) when no alias matches. Most-specific (longest) keyword-set wins."""
    words = set(_norm_label(label).split())
    aliases = data.get("field_aliases") or TEMPLATE["field_aliases"]
    best = None  # (key, specificity)
    for key, sets in aliases.items():
        for kw in sets:
            if kw and all(w in words for w in kw):
                if best is None or len(kw) > best[1]:
                    best = (key, len(kw))
    if best is None:
        return (None, None)
    return (best[0], dig(data, best[0]) or "")


def normalize(q):
    q = q.lower().strip()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    # Drop common filler words that add noise to matching.
    stop = {"the", "a", "an", "your", "you", "to", "of", "for", "is", "are",
            "do", "did", "this", "event", "please", "what", "how", "and"}
    # Drop stopwords and single-char noise ("s" from "what's", stray initials).
    toks = [t for t in q.split() if t not in stop and len(t) > 1]
    return " ".join(toks) if toks else q


def score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    inter = len(ta & tb)
    jacc = inter / len(ta | tb) if (ta | tb) else 0.0
    # Containment: one question's key tokens fully inside the other ("hear about"
    # inside "where did you hear about us"). Slightly discounted so a pure subset
    # doesn't beat an exact match. This is what makes reworded questions reuse.
    contain = (inter / min(len(ta), len(tb))) if min(len(ta), len(tb)) else 0.0
    token_score = max(jacc, contain * 0.95)
    # Blend char-level similarity with token overlap; token overlap handles
    # reordered / partial phrasings ("how did you hear" vs "where did you hear").
    return round(0.45 * seq + 0.55 * token_score, 4)


def cmd_init():
    path = profile_path()
    if os.path.exists(path):
        print(f"exists: {path}")
        return
    save(load())
    print(f"created: {path}")


def cmd_show():
    print(json.dumps(load(), indent=2, ensure_ascii=False))


def cmd_path():
    print(profile_path())


def cmd_get(key):
    val = dig(load(), key)
    print("" if val is None else val)


def cmd_set(key, value):
    data = load()
    put(data, key, value)
    # Keep full_name in sync if first/last set.
    if key in ("identity.first_name", "identity.last_name"):
        fn = dig(data, "identity.first_name") or ""
        ln = dig(data, "identity.last_name") or ""
        joined = (fn + " " + ln).strip()
        if joined and not dig(data, "identity.full_name"):
            put(data, "identity.full_name", joined)
    save(data)
    print(f"set {key} = {value}")


def cmd_missing():
    data = load()
    for key in STANDARD_REQUIRED:
        if not dig(data, key):
            print(key)


def cmd_match(question):
    data = load()
    answers = data.get("custom_answers", {})
    threshold = float(os.environ.get("LUMA_MATCH_THRESHOLD", "0.60"))
    best = {"matched": False, "question": "", "value": "", "score": 0.0}
    for stored_q, ans in answers.items():
        s = score(question, stored_q)
        if s > best["score"]:
            best = {"matched": s >= threshold, "question": stored_q,
                    "value": ans, "score": s}
    print(json.dumps(best, ensure_ascii=False))


def cmd_remember(question, answer):
    data = load()
    data.setdefault("custom_answers", {})
    data["custom_answers"][normalize(question)] = answer
    save(data)
    print(f"remembered: {normalize(question)!r} -> {answer!r}")


def cmd_route(label):
    key, val = route_label(load(), label)
    print(json.dumps({"key": key, "value": val, "known": bool(val)}, ensure_ascii=False))


def cmd_persist_field(label, value):
    """Store any answer (typed, dropdown pick, checkbox) in its routed structured
    key; falls back to custom_answers keyed by the normalized label."""
    data = load()
    key, _ = route_label(data, label)
    if key:
        put(data, key, value)
        dest = key
    else:
        data.setdefault("custom_answers", {})[normalize(label)] = value
        dest = f"custom_answers[{normalize(label)!r}]"
    save(data)
    print(json.dumps({"stored": dest, "value": value}, ensure_ascii=False))


def cmd_forget(question):
    data = load()
    ca = data.get("custom_answers", {})
    k = question if question in ca else normalize(question)
    had = ca.pop(k, None) is not None
    save(data)
    print(json.dumps({"forgot": k, "had": had}, ensure_ascii=False))


def cmd_list_custom():
    data = load()
    for q, a in data.get("custom_answers", {}).items():
        print(f"{q}\t{a}")


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    table = {
        "init": (cmd_init, 0),
        "show": (cmd_show, 0),
        "path": (cmd_path, 0),
        "get": (cmd_get, 1),
        "set": (cmd_set, 2),
        "missing": (cmd_missing, 0),
        "match": (cmd_match, 1),
        "remember": (cmd_remember, 2),
        "list-custom": (cmd_list_custom, 0),
        "route": (cmd_route, 1),
        "persist-field": (cmd_persist_field, 2),
        "forget": (cmd_forget, 1),
    }
    if cmd not in table:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(__doc__)
        return 2
    fn, argc = table[cmd]
    if len(rest) < argc:
        print(f"'{cmd}' needs {argc} argument(s)", file=sys.stderr)
        return 2
    fn(*rest[:argc]) if argc else fn()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
