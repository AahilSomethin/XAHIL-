# Cloud Dry-Run Deployment (Phase 4)

Everything here is a **manual owner step** in the claude.ai web UI or on GitHub. Nothing in this
repo creates, enables or schedules tasks. Order execution stays OFF throughout Phase 4.

## 0. Before you start
- [ ] Fix Telegram (see §5 of [PHASE4-READINESS.md](PHASE4-READINESS.md)): `python -m xahil telegram-setup`, then a real `notify` test.
- [ ] Approve and push the Phase 4 changes, so the cloud clone has the $100,000 baseline and the baseline
      snapshot. GitHub `main` is currently `9a40ec5`, which still expects $1,000.
- [ ] Confirm the GitHub repo is **private** (Settings → General → Danger Zone shows "Change visibility").
- [ ] Confirm `main` has **no** branch protection or ruleset that blocks pushes from your own account
      (Settings → Branches / Rules). The runs push `memory/` commits to `main`.

## 1. GitHub access for Claude
- [ ] Connect GitHub at claude.ai/code and grant access to **only** `AahilSomethin/XAHIL-` (least privilege).

## 2. Cloud environment (create one: "XAHIL paper dry-run")
**Network access → Custom**, allowed domains (keep "Also include default list of common package managers" checked):
```
paper-api.alpaca.markets
data.alpaca.markets
api.tavily.com
api.telegram.org
```
Do not add `api.alpaca.markets` (live trading) or `api.perplexity.ai` (paid, not approved).

**Network secrets** (the agent never sees these values):
| Secret name | Allowed websites | Header(s) | Prefix | Value |
|---|---|---|---|---|
| Alpaca paper | `paper-api.alpaca.markets`, `data.alpaca.markets` | `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` | none | paper key ID (`PK…`) / paper secret |
| Tavily | `api.tavily.com` | `Authorization` | `Bearer` | your `tvly-…` key |

**Environment variables:**
```
XAHIL_CLOUD=1
XAHIL_ALPACA_AUTH=proxy
XAHIL_TAVILY_AUTH=proxy
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_CHAT_ID=<YOUR chat id from `telegram-setup`, not the bot's id>
```
**Never set:** `XAHIL_ORDER_EXECUTION`, `ALPACA_ENDPOINT`, `PERPLEXITY_API_KEY`.

Fallback, if the secret form only accepts one header per secret: use `XAHIL_ALPACA_AUTH=headers` with
variables `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (paper keys only). Likewise `TAVILY_API_KEY` with
`XAHIL_TAVILY_AUTH` unset. Variables are readable by anyone using the environment, which on your
personal account is only you.

The Telegram token travels in the URL, so it can't be a network secret.

## 3. The five scheduled tasks
Create each at claude.ai/code/routines. Paste the prompt file **verbatim** (regenerate with
`python routines/_build.py` first if you edit anything).
After saving, reopen the task and check that every `RUN:` line and `{{...}}` slot is intact
(the editor strips `<...>`; `tests/test_routines.py` simulates it).

| Task name | Prompt file | Schedule (Custom cron, paste exactly) | ET |
|---|---|---|---|
| XAHIL pre-market | `routines/pre-market.md` | `CRON_TZ=America/New_York 7 8 * * 1-5` | 08:07 Mon–Fri |
| XAHIL market-open | `routines/market-open.md` | `CRON_TZ=America/New_York 47 9 * * 1-5` | 09:47 Mon–Fri |
| XAHIL midday | `routines/midday.md` | `CRON_TZ=America/New_York 7 12 * * 1-5` | 12:07 Mon–Fri |
| XAHIL daily-summary | `routines/daily-summary.md` | `CRON_TZ=America/New_York 22 16 * * 1-5` | 16:22 Mon–Fri |
| XAHIL weekly-review | `routines/weekly-review.md` | `CRON_TZ=America/New_York 7 10 * * 6` | 10:07 Sat |

For **every** task:
- [ ] Repository `AahilSomethin/XAHIL-`, branch `main`; environment "XAHIL paper dry-run".
- [ ] **Connectors: remove ALL of them.** New tasks include every connector on your account by default.
      Some of your connectors can place trades or enable trading outside XAHIL's risk engine. XAHIL needs
      no connectors: it reaches Alpaca, Tavily and Telegram only through `python3 -m xahil`.
- [ ] Model: a Sonnet-class model, to make your Pro usage last (about 26 runs a week).
- [ ] Create the task **paused / without enabling the schedule** until the smoke test below passes.

## 4. Smoke test (one run, by hand)
Use **Run now** on *XAHIL pre-market* on a trading day between **06:00 and 09:25 ET**. Outside that
window `run-start` stops on purpose (exit 5) and commits nothing. Then check:
- [ ] The run's transcript shows `preflight` with `paper_endpoint` paper, `auth_mode: proxy`, equity 100000, `execution_mode: dry-run`, Tavily `available`.
- [ ] A commit `run-start pre-market <date>` and a commit `pre-market research <date>` appear on GitHub `main`, touching only `memory/`.
- [ ] `memory/RESEARCH-LOG.md` has a `## <date>` entry with evidence ids, and `memory/state/research_usage.jsonl` grew by at most 8 credits.
- [ ] `git pull` locally, then `python -m xahil orders all` shows **no orders** at Alpaca.

## 5. Enabling the schedules (your decision)
Only after the smoke test passes: enable the five schedules. Execution stays dry-run, because
`config/execution.json` is false and `XAHIL_ORDER_EXECUTION` is unset. Follow
[PHASE4-DRY-RUN-PLAN.md](PHASE4-DRY-RUN-PLAN.md) for the five trading days.

**Stop instantly:** pause the tasks, or add `XAHIL_HALT=1` to the environment.
