<div align="center">

# 🎟️ luma-filler

### Paste a Luma link. It registers you. Stop filling out the same form forever.

[![MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![Claude Code](https://img.shields.io/badge/Claude%20Code-skill-da7756)

</div>

---

It's 11:47pm. You found the perfect hackathon on [Luma](https://lu.ma).

Then the form loads: name, email, phone, LinkedIn, GitHub, job title, *"what have you
built?"*, *"why do you want to attend?"* — for the **tenth time this month**.

**luma-filler does it for you.** Paste the link → it reads the event, fills every field from
your profile, writes real answers from your GitHub, and submits. Then it **remembers** — so
the next event asks you *nothing*.

## ✨ The magic

```text
you →  /luma-filler  https://lu.ma/that-cool-hackathon
       reading the event… drafting your answers…   ✓ registered

       (next event, same questions)
you →  /luma-filler  https://lu.ma/another-one
       registering you from your saved profile…    ✓ done — zero questions
```

First time, it learns you. Every time after, it's one paste and zero typing.

## What it does for you

- 🧠 **Reads the event** — gets what it's about and what the host is screening for
- ⚡ **Fills everything** — name, email, socials, job title… from your saved profile
- ✍️ **Writes your answers** — *"what have you built?"* answered from your **real GitHub
  projects** (grounded in fact, never made up)
- 🔁 **Gets faster every time** — remembers every answer; repeat events go through in one pass
- ✋ **Confirms before submitting** — and only goes hands-off when everything's already known

<sub>Under the hood: a `SKILL.md` workflow + three tiny Python helpers — answer memory,
GitHub research, and a headless Playwright browser. No external services.</sub>

## 🚀 Get started

```bash
git clone https://github.com/mohitpaddhariya/luma-filler.git ~/.claude/skills/luma-filler
python3 -m venv ~/.luma-filler/venv && ~/.luma-filler/venv/bin/pip install playwright
```

It's a [Claude Code](https://claude.com/claude-code) skill. Then just hand it a link:

```text
/luma-filler  https://lu.ma/your-event
```

## 🔒 Your data, your control

- Everything stays **local** in `~/.luma-filler/` — nothing personal lives in this repo.
- It **asks before it submits**; hands-off only when every value is already saved and safe.
- **No CAPTCHA-solving.** If a host throws a bot-check, it hands off to you — and logged in,
  you won't see one.

<sub>For personal use — play nice with Luma's Terms of Service.</sub>

---

<div align="center">
Built by <a href="https://github.com/mohitpaddhariya">Mohit Paddhariya</a> · <a href="LICENSE">MIT</a>
</div>
