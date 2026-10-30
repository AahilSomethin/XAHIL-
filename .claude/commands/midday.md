---
description: Run the midday workflow locally against the PAPER account (no commit, no push)
---

Run the workflow in routines/midday.md with these local differences:
- Use `python -m xahil` instead of `python3 -m xahil` (Windows).
- Credentials come from the local .env file (gitignored). Never print them.
- STEP 0: skip `git pull` and the `persist` call. Still run `run-start`; if it exits 5, tell me why and stop.
- FINAL STEP: run `run-finish` and `guard`, but do NOT run `persist`, commit or push unless I ask.
Everything else, including the SAFETY block, whole-share entries and dry-run behaviour, applies unchanged.
