# luma-filler

**Register for [Luma](https://lu.ma) events automatically — and intelligently.** Paste a
`lu.ma` link and it reads the event, fills the form from a saved profile, drafts grounded
answers to open‑ended application questions, confirms, and submits. It **learns from every
event**, so repeat registrations go through with *zero questions* in a single browser pass.

Built as a [Claude Code](https://claude.com/claude-code) skill — a `SKILL.md` workflow plus
three small, dependency‑light Python helpers.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Playwright](https://img.shields.io/badge/browser-Playwright-2ea44f)

---

## What it does

- **Understands the event.** Reads the page and forms a brief — what it is, who's hosting,
  what the host is screening for — and picks an appropriate tone.
- **Fills from a saved profile.** Name, email, phone, company, job title, socials, city,
  dietary, t‑shirt, pronouns… mapped automatically, never re‑typed.
- **Researches you.** Pulls your public GitHub (top projects, bio) and a LinkedIn URL you
  provide, to answer "what have you built?" / "share a link to a project" with real facts.
- **Drafts open‑ended answers.** For "why do you want to attend?" / "what will you build?",
  it writes a grounded draft (your projects + bio + the event context). Auto‑fills a best
  draft and only asks when genuinely unsure — never fabricates.
- **Handles real‑world forms.** Lazy custom dropdowns, multi‑field application forms,
  approval‑required ("Request to Join") events, paid‑event guardrails, and email‑code login.
- **Learns → gets faster.** Every answer (including dropdown and checkbox picks) is
  remembered. Recurring fields are routed deterministically, so the *second* time an event
  asks the same things, it asks you nothing.
- **Always confirms before submitting** — unless every value already came from your saved
  data, in which case it submits hands‑off.

## How it works

```
luma-filler/
├── SKILL.md                 # the Claude Code skill: the end-to-end workflow
├── profile.example.json     # shape of the saved profile
└── scripts/
    ├── profile.py           # profile store + deterministic field routing + answer memory
    ├── research.py          # GitHub + LinkedIn research cache
    └── browser.py           # self-contained headless Playwright (read / fill / submit / apply)
```

- **`browser.py`** drives a real headless Chromium via [Playwright](https://playwright.dev) —
  no external browser service. It reads a form into structured fields (including capturing
  the options of Luma's lazy custom dropdowns), fills them, and submits. A one‑shot
  `apply` command does read + fill + submit in a single launch.
- **`profile.py`** stores your answers and **routes** form‑field labels to the right profile
  key deterministically (e.g. *"What is your current job title?"* → `professional.job_title`),
  so recurring fields are never guessed or re‑asked. Free‑form questions fall back to fuzzy
  matching against previously remembered answers.
- **`research.py`** caches your public GitHub profile/top repos (and a LinkedIn headline you
  supply) to ground the open‑ended drafts.

All of your data lives locally under `~/.luma-filler/` (profile, research cache, the
persistent login, screenshots). **None of it is in this repo.**

## Install

This is a Claude Code skill. Clone it into your skills directory:

```bash
git clone https://github.com/mohitpaddhariya/luma-filler.git \
  ~/.claude/skills/luma-filler

# One-time: a dedicated venv for the browser client (the other scripts are stdlib-only)
python3 -m venv ~/.luma-filler/venv
~/.luma-filler/venv/bin/pip install playwright
# Chromium is reused from your Playwright cache if present, else:
#   ~/.luma-filler/venv/bin/playwright install chromium
```

> `browser.py` self‑bootstraps into that venv, so you can invoke it with the system
> `python3`. Headless Chromium needs to run outside a restrictive sandbox.

## Usage

In Claude Code, just share a Luma link with intent to attend:

```
/luma-filler https://lu.ma/your-event
```

The first time, it asks for whatever it doesn't know yet (and remembers it). After that:

```
You:  /luma-filler https://lu.ma/another-event-by-the-same-host
Skill: Registering you for "<Event>" from your saved profile — submitting now ✓
```

A one‑time `browser.py login` signs you into Luma in a visible window; being logged in keeps
registrations hands‑off.

## Why it gets faster

The first event for a given set of questions does the work; every event after is fast and
question‑free, because the skill:

1. **Resolves before asking** — deterministic routing for recurring fields, fuzzy match for
   custom prose.
2. **Persists every answer** the instant you give it — including dropdown and checkbox picks.
3. **One‑shot applies** — read + fill + submit in a single browser launch when the profile
   already covers the form.

## Data & safety

- **Your data stays local** in `~/.luma-filler/`. Nothing personal is committed here.
- **Confirmation is the default.** It only auto‑submits when 100% of the values came from
  your saved profile and nothing was freshly drafted or asked this run. New or AI‑drafted
  answers always require your explicit OK. Paid events always stop before payment.
- **No CAPTCHA solving.** If a host‑side bot check appears, the skill hands off to a
  pre‑filled visible browser for you to finish — it does not try to defeat the challenge.
  Being logged in normally avoids it entirely.

## Disclaimer

This is a personal automation tool for registering yourself for events you intend to
attend. Use it responsibly and in line with Luma's Terms of Service.

## License

[MIT](LICENSE) © Mohit Paddhariya
