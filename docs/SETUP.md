# Manual Setup Checklist

Steps only you can do, in order. Nothing here costs money. Execution stays in
dry-run the whole way through.

## Budget (limit: $40/month total)

| Item | Cost | Status |
|---|---|---|
| Claude Pro | $20/mo (you already pay this) | Scheduled runs use your Pro allowance. No API credits needed. |
| Alpaca paper account + IEX market data | $0 | Required |
| Telegram bot | $0 | Required for notifications (falls back to a file) |
| GitHub private repo | $0 | Required |
| Perplexity API | Pay-as-you-go | **Off.** Optional. Needs your approval. |
| Liquid, Quiver Quant | Not integrated | Off |

If Pro counts toward the $40, about $20/month is left. Perplexity's `sonar`
model at about 8 queries per trading day is roughly 175 requests a month. Check
current pricing at perplexity.ai before deciding. If you approve it, buy a small
prepaid credit, keep auto top-up **off**, and leave `monthly_request_cap`
(150 by default) in `config/integrations.json` as a second limit.

## 1. Local install (5 min)
- [ ] Python 3.10+ is installed (you have 3.11 and 3.13). No packages are needed.
- [ ] Run the tests: `python -m unittest discover -s tests -t .` (should print `OK`).

## 2. Alpaca paper account
- [ ] Sign in at alpaca.markets and switch the dashboard to **Paper Trading**.
- [ ] **Reset the paper account balance to $1,000.** It defaults to $100,000.
      Buys stay blocked while equity is above $3,000, on purpose. Do this before the
      first real `reconcile`. If you reset later, XAHIL detects it, halts, and you run
      `python -m xahil reset-baseline` in your own terminal to start fresh.
- [ ] Optional but recommended: in paper account settings, turn off shorting
      and set the max margin multiplier to 1. The code already enforces cash
      only and long only; this adds a second layer.
- [ ] Generate **paper** API keys. The key ID starts with `PK`. Never create live keys for this project.

## 3. Telegram bot
- [ ] In Telegram, message `@BotFather`, send `/newbot`, and copy the token.
- [ ] Send any message to your new bot, then open
      `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
      `"chat":{"id": ...}`.

## 4. Local smoke test
- [ ] Copy `.env.example` to `.env` and fill in the paper keys and Telegram values.
- [ ] `python -m xahil preflight`: should show `paper_endpoint`, `number_prefix: "PA"`, `execution_mode: "dry-run"`.
- [ ] `python -m xahil market-status`
- [ ] `python -m xahil reconcile --workflow smoke-test` (exit 0 once the balance is $1,000)
- [ ] `python -m xahil notify "XAHIL test message"`: arrives in Telegram.
- [ ] Optional: in Claude Code, run `/portfolio`, then `/pre-market`.

## 5. Commit and push this foundation
- [ ] Review the diff, then commit and push to `main` on GitHub. Keep the repo **private**.

## 6. Cloud scheduled tasks (only after steps 1–5 pass)
Follow `routines/README.md`:
- [ ] Connect GitHub to Claude (claude.ai/code), with access to this repo only.
- [ ] Create one cloud environment: Custom network allowlist, an Alpaca network
      secret (or paper-key variables), Telegram variables, `XAHIL_CLOUD=1`.
- [ ] Create the five tasks with the exact `CRON_TZ=America/New_York ...` crons.
- [ ] Click **Run now** on pre-market once, between 06:00 and 09:25 ET on a trading day, and
      confirm a commit lands in `memory/`. Outside that window `run-start` stops the run on
      purpose (exit 5), and nothing is committed.
- [ ] Watch claude.ai/settings/usage for the first week.

## 6b. Dry-run track record and activation check
- [ ] Run the workflows in dry-run for at least 5 trading days and one weekly review (locally with
      `/pre-market` etc., or as cloud tasks with execution off).
- [ ] Read `memory/backtests/` and the weekly "Learning analysis". The current backtest is **inconclusive**.
- [ ] Decide the PENDING items in `memory/STRATEGY-PROPOSALS.md` (signal filter, regime gate, universe).
- [ ] `python -m xahil activation-check`: every blocking item must be ok.

## 7. Order execution: NOT ACTIVATED
The bot proposes orders and logs them as dry runs. To approve real paper order
execution later, set both switches yourself (`config/execution.json` and the
`XAHIL_ORDER_EXECUTION=enabled` environment variable). Details in `routines/README.md`.
Run a few weeks of dry runs first and read the weekly reviews.
