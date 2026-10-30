# Audit of "Opus 4.7 Trading Bot — Setup Guide" (Nate Herk, 28 pp.)

Audited 2026-10-30 against your requirements, current Claude Code scheduled-task
docs (code.claude.com/docs/en/routines, /cloud-environments) and Alpaca's
fractional-trading docs. Severity: **Critical** = could lose real money or
leak secrets; **High** = breaks a requirement or will fail in practice;
**Medium** = reliability/accuracy; **Low** = polish.

Each finding lists what XAHIL does instead.

## 1. Unsafe defaults

| # | Sev | Guide says | Problem | XAHIL fix |
|---|-----|-----------|---------|-----------|
| U1 | Critical | `ALPACA_ENDPOINT` optional, **defaults to `https://api.alpaca.markets/v2` (live)** (pp. 9, 14, 15) | A missing env var silently means real-money trading. | Endpoint is a code constant `https://paper-api.alpaca.markets/v2`. If `ALPACA_ENDPOINT` is set to anything else (even a trailing slash), every command exits non-zero. The HTTP client re-checks every URL and refuses redirects. |
| U2 | Critical | Persona: "managing a **LIVE** ~$10,000 account", "places real trades" (pp. 1, 13, 18) | Primes the agent to treat live trading as normal. | All prompts say PAPER ONLY. A test fails the build if a live Alpaca URL appears in code, prompts or templates. |
| U3 | Critical | Raw `alpaca.sh order '<json>'` passes any JSON to Alpaca (p. 16) | The "buy-side gate" is just prompt text. Nothing in code stops options, shorts, oversized or leveraged orders. | Every order goes through `python -m xahil order ...`, which runs a deterministic risk engine first. There is no raw-JSON passthrough. |
| U4 | Critical | `close-all` and `cancel-all` subcommands (p. 16) | One bad tool call liquidates everything or strips every stop. | Not implemented. |
| U5 | High | Agent edits `TRADING-STRATEGY.md` on its own in the weekly review (pp. 7, 23) | Breaks requirement 10. Also lets prompt-injected research rewrite the rules. | The weekly review writes `memory/STRATEGY-PROPOSALS.md` only. `xahil guard` refuses agent commits that touch the strategy, config, code or prompts. |
| U6 | High | "Allow unrestricted branch pushes" plus pushing to `main` (p. 9) | The cloud agent can push code changes, including changes to its own safety checks. | `xahil guard` runs before every commit, and prompts only stage `memory/`. **Caveat:** an agent with shell access could still bypass a check that runs in its own sandbox. The paper-only credentials are the backstop: no live key exists anywhere. |
| U7 | High | `.env` loaded with `set -a; source .env` (pp. 15–18) | `source` runs any shell code in that file. | `.env` is parsed as plain `KEY=VALUE` text and never executed. Cloud runs refuse to read `.env` at all. |
| U8 | Medium | ClickUp fallback writes `DAILY-SUMMARY.md` at the repo root, uncommitted (p. 17) | Messages are lost when the container is destroyed. | Telegram fallback appends to `memory/NOTIFICATIONS-OUTBOX.md`, which is committed. |
| U9 | Medium | Every order is a market order at the open (pp. 5, 20) | Opening-auction slippage, and cost estimates are wrong. | Run at 09:47 ET. Risk engine adds a 2% price buffer to the cost estimate and rejects spreads above 1% and prices under $1. |
| U10 | Medium | Guide is "self-contained … paste into Claude" | Research text from Perplexity or the web is untrusted (prompt injection). | Prompts treat research as data. The risk engine is the backstop whatever the agent "decides". |

## 2. Outdated assumptions

| # | Sev | Guide says | Current reality (Oct 2026) | XAHIL fix |
|---|-----|-----------|---------------------------|-----------|
| O1 | High | Set a timezone on the routine; crons such as `0 6 * * 1-5` in Chicago (p. 10) | Scheduled tasks (renamed from "routines") read a 5-field cron as **UTC**, unless it starts with `CRON_TZ=`. | All crons in `routines/README.md` start with `CRON_TZ=America/New_York`. |
| O2 | High | Put API keys in routine environment variables (p. 9) | Docs now say environment variables are readable by anyone who uses the environment, and recommend **network secrets**. These are injected by Anthropic's proxy, so the agent never sees the key. | Supports `XAHIL_ALPACA_AUTH=proxy`: requests go out with no auth headers and the proxy attaches the paper keys, only for `paper-api.alpaca.markets` and `data.alpaca.markets`. |
| O3 | High | Cloud runs can reach any API | The default environment is **Trusted** (package registries only). Alpaca, Telegram and Perplexity would be blocked. | Setup guide lists the exact domains for a Custom allowlist. Hosts named on a network secret are reachable automatically. |
| O4 | High | Runs push straight to `main` once a toggle is on | Runs push to `claude/`-prefixed branches unless the prompt directs otherwise. Pushes to `main` are governed by GitHub branch protection. | Prompts push to `main` explicitly. Setup tells you to leave `main` unprotected for the bot, or move to a bot branch later. |
| O5 | High | 10% **trailing stop GTC** on every position (pp. 3, 5, 20) | With $1,000 and a 5% cap, positions are ≤ $50, so mostly **fractional shares**. Alpaca fractional orders support only market, limit, stop and stop-limit with **DAY** time-in-force. No trailing stops, no GTC. | Whole-share positions may use a GTC trailing stop. Fractional positions get a fresh DAY stop order each market-open, plus software checks at midday. The risk engine rejects trailing stops on fractional quantities. |
| O6 | Medium | Shell scripts call `python -c` (pp. 17–18) | Many Linux containers only provide `python3`, and Windows has no bash by default. | Pure-Python CLI (standard library only, no pip install), run with `python3 -m xahil` in the cloud and `python -m xahil` on Windows. |
| O7 | Medium | Starting capital ~$10,000; 20% per position; 75–85% deployed (pp. 2, 13) | Conflicts with your limits (5% per position, 20% total exposure). | Strategy rewritten around $1,000, 5% / 20% caps, and at most 4 positions. |
| O8 | Medium | "Opus 4.7" model | Model names change. On Pro, every run uses your subscription usage. | No model is pinned. Setup recommends a Sonnet-class model to stretch Pro limits. |
| O9 | Low | Alpaca paper accounts are "fine to start" at $10k | Paper accounts default to $100,000. | Fail-closed check: buys are blocked while equity is above 3× the configured $1,000. Setup tells you to reset the paper balance to $1,000. |

## 3. Missing safeguards

| # | Sev | Gap | XAHIL fix |
|---|-----|-----|-----------|
| M1 | Critical | No check that the key or account is paper | Key IDs must start with `PK` (Alpaca paper prefix), and the account number must start with `PA`. Otherwise everything fails closed. |
| M2 | High | No idempotency: a re-run of market-open can double-buy | Deterministic `client_order_id` per day/workflow/symbol/side. Alpaca rejects duplicates. |
| M3 | High | Weekly trade count comes from parsing a markdown log | Counted from Alpaca's actual buy orders since Monday 00:00 ET. |
| M4 | High | Nothing stops a sell from exceeding the held quantity (which creates a short) | Sell quantity plus quantity reserved by open sell orders must be ≤ the position. Any short at the broker halts all buying. |
| M5 | High | Uses `buying_power`, which on a margin paper account is 2–4× cash | Cash-only: cost ≤ cash minus open buy orders. Negative cash halts buying. |
| M6 | High | No block on leveraged/inverse ETFs (embedded leverage or shorting) | Denylist plus name check ("2X", "3X", "Ultra", "Inverse", "Bear", …) and an exchange allowlist. OTC and crypto are rejected. |
| M7 | High | No market-hours or holiday check; cron fires on holidays | `xahil market-status` checks the Alpaca clock and calendar. Routines exit early on holidays. |
| M8 | High | Midday step closes the position **then** cancels the stop (p. 21) | Alpaca reserves shares for the open stop, so the close fails. XAHIL cancels the stop first, then sells. |
| M9 | Medium | Partial-failure reconciliation is only mentioned (p. 8) | `xahil reconcile` runs at the start of every workflow, diffs the broker against the last committed snapshot, logs differences, and sets halt conditions. |
| M10 | Medium | Day P&L parsed from yesterday's markdown | Equity history in `memory/state/equity_history.csv`, plus Alpaca's `last_equity`. |
| M11 | Medium | S&P 500 weekly return "via Perplexity" (p. 23), which an LLM can get wrong | SPY daily closes from Alpaca's free IEX bars. Deterministic. |
| M12 | Medium | No drawdown, no benchmark since inception | Max drawdown, cumulative return vs SPY, win rate, and profit factor from FIFO-matched fills. |
| M13 | Medium | Perplexity has no spending cap | Off by default. When on, a monthly request cap is enforced in code (`config/integrations.json`). |
| M14 | Medium | No kill switch | `config/execution.json` `halt: true` or env `XAHIL_HALT=1` blocks all new orders. |
| M15 | Low | Telegram/ClickUp token could be echoed in errors | Tokens are redacted from all error output. |

## 4. Scheduling conflicts

| # | Sev | Conflict | XAHIL schedule (`CRON_TZ=America/New_York`) |
|---|-----|---------|------|
| S1 | High | Market-open at 8:30 CT is the exact opening bell: wide spreads, queued orders | `47 9 * * 1-5` (09:47 ET) |
| S2 | High | Daily summary at 3:00 PM CT is the exact close, so closing prices aren't final | `22 16 * * 1-5` (16:22 ET) |
| S3 | High | Weekly review on Friday misses Good Friday and other Friday holidays, and runs only an hour after the daily summary | `7 10 * * 6` (Saturday 10:07 ET). Reviews the week whatever day it ended. |
| S4 | Medium | Midday at 12:00 CT is 1:00 PM ET, which is exactly the close on half-days (day after Thanksgiving, Christmas Eve) | `7 12 * * 1-5` (12:07 ET, before any early close) |
| S5 | Medium | On-the-hour schedules may start several minutes late (per docs) | All jobs at :07, :22 or :47 past the hour |
| S6 | Medium | Guide crons are in Central time but Alpaca reports UTC; the guide converts by hand | ET throughout. Week boundaries come from the Alpaca clock's own offset. |
| S7 | Low | Pre-market at 6:00 CT gives little pre-market data | `7 8 * * 1-5` (08:07 ET) |

Five workflows add up to about 26 runs a week, well under the 100-per-hour
scheduled-run limit. The real constraint is **Pro subscription usage**: each
run uses the same allowance as your interactive Claude Code sessions. Watch
claude.ai/settings/usage during the first week.

## 5. What was kept from the guide

- Claude Code is the agent. No separate bot process. Stateless runs, with Git as memory.
- The five workflows, the 8-step prompt scaffold, the loud persistence warning, rebase-on-conflict and never force-push.
- The memory files (strategy, trade log, research log, weekly review, project context).
- The notification philosophy (quiet unless something happened; always send the daily and weekly messages).
- Swing-trading discipline: catalyst required, −7% cut, trailing-stop tightening at +15% / +20%, exit a sector after two failures, patience over activity. These are **strategy** rules followed by Claude. Your 12 requirements are **risk** rules enforced in code.
