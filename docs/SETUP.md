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
| Tavily search API | $0 (free tier: 1,000 credits/month, no card) | **Primary research.** XAHIL caps it at 900/month and 40/day; basic search only |
| Perplexity API | Pay-as-you-go | **Off.** Optional fallback. Needs your approval. |
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
- [ ] **Keep the $100,000 paper balance** (owner decision 2026-11-01; `starting_capital` is 100000).
      Do NOT reset the account: XAHIL would detect the change, halt, and need
      `python -m xahil reset-baseline` in your own terminal. Buys halt if equity is ever above $300,000.
- [ ] Optional but recommended: in paper account settings, turn off shorting
      and set the max margin multiplier to 1. The code already enforces cash
      only and long only; this adds a second layer.
- [ ] Generate **paper** API keys. The key ID starts with `PK`. Never create live keys for this project.

## 3. Telegram bot
- [ ] In Telegram, message `@BotFather`, send `/newbot`, and copy the token.
- [ ] Send any message to your new bot, then open
      `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
      `"chat":{"id": ...}`.

## 3b. Tavily (free research)
- [ ] Sign up at tavily.com on the free plan. Don't add a card or enable pay-as-you-go.
- [ ] Copy the API key into `.env` as `TAVILY_API_KEY=tvly-...`.
- [ ] `python -m xahil preflight` should show `research.tavily.status: available`.
- [ ] `python -m xahil research "Ford Motor earnings" --topic news` (1 credit) should list dated sources.

## 3c. Telegram chat id
- [ ] `TELEGRAM_CHAT_ID` must be YOUR chat id, not the bot's. Message the bot, then run
      `python -m xahil telegram-setup` and copy the `chat_id` it lists into `.env`.

## 4. Local smoke test
- [ ] Copy `.env.example` to `.env` and fill in the paper keys and Telegram values.
- [ ] `python -m xahil preflight`: should show `paper_endpoint`, `number_prefix: "PA"`, `execution_mode: "dry-run"`.
- [ ] `python -m xahil market-status`
- [ ] `python -m xahil reconcile --workflow smoke-test` (expect exit 0 at $100,000)
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
      confirm a commit lands in `memory/` on branch `claude/xahil-state` (never `main`). Outside that
      window `run-claim` stops the run on purpose (exit 5), and nothing is committed.
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
