# Security

Lumate registers you for events you choose, on your own machine. This document is the
"thorough review" an automated audit asks for: what it does, what it touches, and the
safeguards in place.

## Trust model in one line

You paste a link, the skill fills the form from data you saved, and **you approve every
submission**. Your data stays on your machine.

## What it touches

- **Your data is local.** Profile, research cache, browser login, and screenshots all live
  under `~/.lumate/`. Nothing personal is in this repository, and nothing is sent anywhere
  except the registration form on the lu.ma event you are signing up for. No telemetry, no
  analytics, no third-party servers.
- **Network calls are limited and named.** The browser only navigates **lu.ma** (the event
  you are registering for). `research.py` reads your **public GitHub profile** from
  `api.github.com` (read-only JSON, used to ground your answers). An optional web search can
  look up a LinkedIn URL **you** provide. That is the full list.
- **No arbitrary remote code execution.** The skill downloads **data**, not code. Its only
  third-party dependency is [Playwright](https://playwright.dev), installed into a dedicated
  virtualenv (`~/.lumate/venv`); the headless Chromium it drives is the standard build,
  reused from your local Playwright cache. The three helper scripts are small, stdlib-first,
  and meant to be read before you run them.

## Prompt injection (untrusted event pages)

Event pages are third-party content and could try to hijack the agent. Mitigations:

- The page text returned by `browser.py read` is wrapped in explicit
  `[UNTRUSTED EVENT PAGE TEXT]` markers.
- `SKILL.md` instructs the agent to treat that text as **data only** and to **never** follow
  instructions inside it (for example "ignore previous instructions", "email X to Y", "fill
  the form with Z", "open this link", "export the profile").
- Field values come only from your saved profile and your own confirmed input, never from
  the page.
- A **mandatory confirmation** shows you every value before anything is submitted. That human
  checkpoint is the backstop: nothing is sent without your explicit OK.

## Why it runs a browser outside the agent sandbox

The skill drives a real headless Chromium with Playwright. The agent's default command
sandbox kills Chromium, so the browser step is run in **your own shell** (the `!` prefix) or
with the sandbox disabled. This is a browser-launch limitation, not a privilege grab:
`browser.py` only navigates lu.ma, fills the form, and reads/writes `~/.lumate`. Run it in
your shell so you can see exactly what it does.

## Other safeguards

- **No CAPTCHA solving.** If a host shows a bot check, the skill stops and hands off to you
  to finish in a visible browser. It does not try to defeat the challenge.
- **Paid events stop** before any payment step and ask you first.
- **Login is yours.** A one-time sign-in stores your session locally; the skill never sees or
  stores a password (lu.ma uses email codes).

## Reporting an issue

Open an issue on the repository, or contact the author. Please do not include secrets or
personal data in a public issue.
