# XAHIL - AI Trading Agent (paper only)

A Claude Code swing-trading agent on an **Alpaca paper account with $1,000 of
virtual money**. Adapted from Nate Herk's "Opus 4.7 Trading Bot - Setup Guide":
same five workflows, Git-backed memory and weekly reviews, with your safety
requirements enforced in code.

- **Status:** foundation built and tested. Order execution is **off** (dry-run).
  Scheduled tasks are **not** created yet.
- **Start here:** [docs/SETUP.md](docs/SETUP.md). What changed from the guide and why: [docs/AUDIT.md](docs/AUDIT.md).

## How it works
```
cloud scheduled task (cron, ET)                     Alpaca PAPER
   └─ Claude reads memory/ ─┐                        ▲ only via
                            ├─ python3 -m xahil ─────┘ paper-api / data hosts
   research (WebSearch, or  │     ├─ reconcile (every run; broker = truth)
   Perplexity if approved)  │     ├─ risk engine (every order; deterministic)
                            │     ├─ execution gate (2 switches, default dry-run)
                            │     ├─ metrics (P&L, drawdown, win rate, vs SPY)
                            │     └─ Telegram notify (fallback: outbox file)
   └─ guard → commit memory/ → push main
```

| Workflow | When (ET) | Does |
|---|---|---|
| pre-market | 08:07 Mon–Fri | Research, write dated ideas, check them against the risk engine |
| market-open | 09:47 Mon–Fri | Buys from today's research (dry-run), daily protective stops |
| midday | 12:07 Mon–Fri | −7% cuts, tighten trails, thesis-break exits |
| daily-summary | 16:22 Mon–Fri | EOD snapshot, P&L vs SPY, Telegram recap |
| weekly-review | 10:07 Sat | Computed weekly stats, grade, strategy *proposals* |

## Layout
```
xahil/        risk engine, paper-only Alpaca client, CLI, metrics, reconcile, Telegram, research
config/       human-owned limits, execution switch, strategy params, denylist, integrations
routines/     5 cloud prompts (paste verbatim) + schedule/environment guide
.claude/commands/  local slash commands (/portfolio, /trade, /pre-market, ...)
memory/       agent memory: logs, reviews, proposals, state/ (equity history, snapshots)
tests/        107 offline tests (fake Alpaca; no network)
docs/         AUDIT.md, SETUP.md
```

## Commands
```
python -m unittest discover -s tests -t .
python -m xahil preflight
python -m xahil reconcile --workflow manual
python -m xahil check-buy AAPL --notional 40
```
