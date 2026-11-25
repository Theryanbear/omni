# OMNI: Jarvis + mail engine

Ryan runs OMNI Vehicle Management (fleet washing, Elizabethtown and Louisville KY). This repo holds:

- `jarvis/jarvis.html`: the Jarvis desktop app (one file: HTML, CSS, JS). Published as the Claude artifact
  https://claude.ai/artifact/LrfoaHq8EZto961LeDDp5g. Read that artifact before publishing to it, then publish
  `jarvis/jarvis.html` to the same url. The mail engine ships beside it as `engine/manifest.json` and
  `engine/omni_mail/*.py` (contentType text/plain); Jarvis installs or updates the engine on Ryan's Mac from those.
- `omni_mail/`: the self-hosted cold email engine (Python 3.9+, stdlib only, SQLite). Runs on Ryan's Mac in
  `~/omni-mail` as a launchd worker. JSON API: `python3 -m omni_mail api` with `{"op": ..., "args": ...}` on stdin.
- `tests/`: `python3 -m pytest -q`. Keep them green.

## Rules for changes
- Keep Jarvis's look (dark HUD, cyan, Oxanium/Rajdhani), its Notion saved views (never SQL or rows mode: they use
  the workspace query allowance), and every approval step (inbox, campaign, sending mode, Mac commands, agents).
- Never add anything that texts people, sends email without Ryan's approval, or stores passwords outside `~/omni-mail/.env`.
- If you change `omni_mail/`, bump `__version__` in `omni_mail/__init__.py` and rewrite `jarvis/engine/manifest.json`.
- Check before publishing: tests pass, `node --check` on the page script, render the page in headless Chromium.
- Notion is the shared brain (OMNI HQ, Shared Memory, Cowork Board, Jarvis Log, Outreach Tracker and Playbook).
  Report finished upgrades as a Cowork Board row (From Cowork, To Ryan, Status Done, Result = what changed).

## Cold email rules (OMNI Playbook)
No dollar figures, no dashes, no links, never "figured", never claim OMNI brings its own water, under 90 words,
plain lowercase subjects. Footer must keep the postal address and an opt out line.
