# Phase 4: Five-Trading-Day Dry-Run Validation Plan

**Goal:** prove the five workflows run unattended, on time, safely, and persist to Git, with **zero
orders reaching Alpaca**. Execution stays OFF the whole week.

**Window:** the first full trading week after you enable the schedules. If you start immediately:
Mon 2026-11-02 to Fri 2026-11-06 (NYSE open all five days, including Election Day, Nov 3), with the
weekly review on Sat 2026-11-07. US daylight saving ended Nov 1; `CRON_TZ=America/New_York` handles
the shift.

## Daily schedule (ET) and what to expect
| Time | Run | Expected output (all in `memory/`, one commit per run plus the run-start marker) |
|---|---|---|
| 08:07 | pre-market | regime and signals recorded; ≤8 Tavily credits; `## <date>` research entry with evidence ids; decisions logged; Telegram only if urgent |
| 09:47 | market-open | protection step (nothing to protect while flat); buys logged as **PROPOSED (dry-run)**, or none; Telegram only if a proposal was made |
| 12:07 | midday | `midday-scan` (no positions → no actions); quiet |
| 16:22 | daily-summary | equity-history row at $100,000 ± 0; EOD block in TRADE-LOG; **one Telegram EOD message, always** |
| Sat 10:07 | weekly-review | `weekly-stats` + `learn` tables; first-of-month backtest; grade; proposals stay PENDING; one Telegram message |

## Your 5-minute daily check
1. Read the Telegram EOD message (it should arrive by about 16:40 ET).
2. `git fetch origin claude/xahil-state`, then `git log --oneline -12 origin/claude/xahil-state -- memory/` to confirm 4 runs, each with a run-claim commit and a result commit; `python -m xahil state-audit` is ok.
3. `python -m xahil orders all` should return `[]`. Any order at all = **abort** (see below).
4. `python -m xahil preflight` shows Tavily credits for the day (≤ 40) and no persistent halts.
5. Glance at claude.ai/settings/usage (Pro allowance).

## Day-by-day focus
| Day | Extra thing to verify |
|---|---|
| Day 1 (Mon) | All 4 runs fired inside their windows; run-claim markers in `memory/state/runs/<date>/` on `claude/xahil-state`; quote freshness and spread look sane in the market-open `check-buy` output (first real in-session IEX data) |
| Day 2 (Tue) | Pre-market reuses memory from Day 1; no duplicate runs; evidence has publication dates |
| Day 3 (Wed) | Read one full research entry: are claims cited by evidence id? Is "insufficient research → HOLD" respected? |
| Day 4 (Thu) | `python -m xahil learn` locally (read-only) shows signals accumulating |
| Day 5 (Fri) | 5 completed daily-summary markers exist |
| Sat | Weekly review written; `python -m xahil activation-check` now passes `dry_run_track_record` |

## Success criteria (all required)
- [ ] 20/20 weekday runs completed (`memory/state/runs/*/<workflow>.json` with `status: completed`), or every miss explained (holiday, deliberate pause).
- [ ] 1/1 weekly review completed.
- [ ] **0 orders** at Alpaca (`orders all` empty) and 0 entries in `memory/state/executed_orders.jsonl`.
- [ ] 0 persist failures, and 0 commits touching anything outside `memory/`.
- [ ] 0 unexplained persistent halts (`python -m xahil halts`).
- [ ] Tavily usage ≤ 40 credits/day and ≤ 200 for the week; never hit HTTP 432.
- [ ] Every proposal has linked evidence; no proposal from insufficient research.
- [ ] 5/5 Telegram EOD messages received, plus 1 weekly message.
- [ ] `activation-check`: all blocking items OK.

## Abort immediately (pause all five tasks) if
- Any order appears at Alpaca, or `executed_orders.jsonl` gets a line.
- A commit touches code, config, routines or `.env`, or contains a key.
- Any run reports `CONFIG/SAFETY ERROR`, or persist fails twice in a row.
- A persistent halt appears that you can't explain.
- Tavily reports its plan limit, or credit use exceeds about 60 a day.

## After the week
1. Review `memory/WEEKLY-REVIEW.md`, the `learn` output and `memory/STRATEGY-PROPOSALS.md`.
2. Decide the three PENDING proposals (current recommendation: keep signals and regime advisory).
3. Decide whether to run longer in dry-run (recommended: at least 4 weeks, given the inconclusive backtest)
   before even considering execution approval, which is a separate decision with both switches set by you.
