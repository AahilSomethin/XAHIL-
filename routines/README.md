# Cloud scheduled tasks (formerly "routines")

Each `*.md` file here is pasted **verbatim** into one Claude Code cloud scheduled
task at [claude.ai/code/routines](https://claude.ai/code/routines). Don't
paraphrase: the SAFETY block and the FINAL STEP are what make the runs safe.

> **Status: NOT ACTIVATED.** Create these tasks only after the local smoke test
> in `docs/SETUP.md` passes. Even then, every order command stays in **dry-run**
> until you approve execution (see "Turning on order execution" below).

## Schedule

Cron expressions are UTC unless they start with `CRON_TZ=`, so keep the prefix.
Minutes are set off the hour because on-the-hour runs can start several minutes late.

| Task | Cron (paste exactly) | ET time | Why this time |
|---|---|---|---|
| pre-market | `CRON_TZ=America/New_York 7 8 * * 1-5` | 08:07 Mon–Fri | Pre-market news and futures are available |
| market-open | `CRON_TZ=America/New_York 47 9 * * 1-5` | 09:47 Mon–Fri | Skips the volatile opening auction and wide spreads |
| midday | `CRON_TZ=America/New_York 7 12 * * 1-5` | 12:07 Mon–Fri | Before the 13:00 early close on half-days |
| daily-summary | `CRON_TZ=America/New_York 22 16 * * 1-5` | 16:22 Mon–Fri | After the 16:00 close, so prices are final |
| weekly-review | `CRON_TZ=America/New_York 7 10 * * 6` | 10:07 Saturday | Week is final; still runs when Friday is a holiday |

Each run checks the Alpaca calendar and does nothing on market holidays.
About 26 runs a week. Each one uses your Claude Pro allowance, the same as an
interactive session. Pick a Sonnet-class model for these tasks to make the
allowance last, and watch claude.ai/settings/usage during the first week.

## Cloud environment (one environment shared by all five tasks)

1. **Network access → Custom**, allowed domains:
   `paper-api.alpaca.markets`, `data.alpaca.markets`, `api.telegram.org`
   (add `api.perplexity.ai` only if you approve Perplexity). Keep "Also include
   default list of common package managers" checked.
2. **Network secrets** (recommended; the agent never sees these keys):
   - Secret "Alpaca paper": allowed websites `paper-api.alpaca.markets` and
     `data.alpaca.markets`. Two custom headers with **no prefix**:
     `APCA-API-KEY-ID` = your *paper* key ID and `APCA-API-SECRET-KEY` = your
     *paper* secret.
   - (Optional, later) "Perplexity": host `api.perplexity.ai`, header
     `Authorization`, prefix `Bearer`.
3. **Environment variables**:
   ```
   XAHIL_CLOUD=1
   XAHIL_ALPACA_AUTH=proxy
   TELEGRAM_BOT_TOKEN=<from @BotFather>
   TELEGRAM_CHAT_ID=<your chat id>
   ```
   Telegram puts its token in the URL, not a header, so it can't be a network
   secret. Anyone who can use this environment can read it. On a personal Pro
   account that is only you. Worst case if it leaks: someone can post to your bot.
   If network secrets don't work for you, set `XAHIL_ALPACA_AUTH=headers` and add
   `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (paper keys only; they start with `PK`)
   as variables instead.
   **Never** set `ALPACA_ENDPOINT` to anything except
   `https://paper-api.alpaca.markets/v2`. Simplest: don't set it at all.
4. **Do not** set `XAHIL_ORDER_EXECUTION` yet.

## Repository and pushes

- Select this repo, branch `main`. Runs push memory commits with
  `git push origin HEAD:main`, so `main` must accept pushes from your GitHub
  identity. Don't add a branch-protection rule that blocks yourself.
- Only `memory/` changes are committed. `python3 -m xahil guard` refuses
  anything else. Review the bot's commits regularly (`git log -p -- memory/`).

## Turning on order execution (later, only with your explicit approval)

Execution needs **two** switches, both set by you:

1. Edit `config/execution.json` yourself:
   `"order_execution_enabled": true`, `"approved_by": "<your name>"`,
   `"approved_on": "<date>"`. Commit and push it.
2. Add `XAHIL_ORDER_EXECUTION=enabled` to the cloud environment's variables.

To stop instantly: set `"halt": true` in `config/execution.json`, or add
`XAHIL_HALT=1` to the environment, or pause the tasks.
