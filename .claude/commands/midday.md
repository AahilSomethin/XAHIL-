---
description: Run the midday workflow locally against the PAPER account (no push)
---

Run the workflow in routines/midday.md with these local differences:
- Use `python -m xahil` instead of `python3 -m xahil` (Windows).
- Credentials come from the local .env file (gitignored). Don't print them.
- In the FINAL STEP run guard and commit, but do NOT push unless I ask.
Everything else, including the SAFETY block and dry-run behaviour, applies unchanged.
