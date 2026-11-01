# Phase 4 Readiness: Cloud Dry-Run Deployment

| | |
|---|---|
| Date | 2026-11-01 (Sat Oct 31 evening ET; US clocks changed Nov 1) |
| Code | GitHub `main` = `9a40ec5` (your Tavily commit, pushed) + uncommitted Phase 4 changes below |
| Baseline | **$100,000** (owner decision: existing Alpaca paper account kept; not reset) |
| Execution | **OFF**: `order_execution_enabled: false`, `XAHIL_ORDER_EXECUTION` unset. No orders. No schedules. Nothing pushed |

**Verdict: READY for cloud dry-run once you finish 5 manual steps (§5).** Telegram delivery is the
one functional blocker: the chat ID is misconfigured.

## 1. $100,000 baseline: what changed
| Area | Change |
|---|---|
| `config/risk_limits.json` | `starting_capital` 1000 → **100000**. Percentages unchanged: 5% per position (~$5,000), 20% total (~$20,000), 3 buys/week, 4 positions |
| `xahil/config.py`, `xahil/backtest.py` | defaults → $100,000 |
| Halt rule | still halts above 3× baseline ($300,000); message now "unexpected deposit, reset or wrong account - investigate" |
| Activation check | baseline band 0.5×–3× = $50,000–$300,000 |
| Unchanged | paper-only endpoint/key/account checks, cash-only sizing (ignores the 4× margin buying power), long-only, whole shares + GTC protection, research gate |
| Text | CLAUDE.md, README, routine prompts (regenerated), TRADING-STRATEGY, PROJECT-CONTEXT, TRADE-LOG Day 0, SETUP, routines README, GUIDE-CONFORMANCE, universe note; AUDIT and the Phase 2/3 reports got "superseded baseline" notes (kept as history) |
| Tests | 9 new baseline tests; CLI "unexpected balance" cases moved from $100k to $400k; pure-engine tests keep an explicit $1,000 fixture |

Consequence: at $100,000 the whole-share rule no longer restricts the universe (6 SPY shares fit in $5,000).

## 2. Verification results (actually run)
| Check | Result |
|---|---|
| Tests | **277 passed**: Python 3.13 unittest; 3.13 pytest (241 subtests); 3.11 unittest |
| Cloud-like clean copy (no `.env`, no credentials, `XAHIL_CLOUD=1`, Python 3.11) | **277 passed**; proxy auth without a proxy fails closed for Alpaca and Tavily (clean 401); guard OK; union merge only on append-only logs |
| Portability | standard library only; all files LF in Git; no Windows paths |
| `preflight` (real) | paper endpoint · `PA` account · ACTIVE · equity **$100,000** · baseline 100000 · dry-run · Tavily available (2/900 credits) · Perplexity off |
| `reconcile --workflow phase4-baseline` (real) | **exit 0**, no halts, no positions or orders; first baseline snapshot written to `memory/state/broker_snapshot.json`. The snapshot now stores a one-way **account fingerprint**, never the raw account number, which appears nowhere in memory or pending changes |
| `activation-check` | every blocking item OK **except** `dry_run_track_record` (0/5 days). Telegram now shows NOT OK (see below) |
| Telegram | **FAILED**: `TELEGRAM_CHAT_ID` is the bot's own ID (@Veyron_Trade_Bot), and Telegram replied "bot can't send messages to the bot". No private chat has messaged the bot yet. Added a `telegram-setup` helper and a preflight/activation check that flags this |
| GitHub | `main` = `9a40ec5` on GitHub; `.env` and the earlier exposed key appear in **no** commit. `gh` CLI not installed, so I couldn't check repo visibility or branch protection |
| Backtest at $100k (read-only data) | in-sample −8.4% (PF 0.61), out-of-sample +2.9% (PF 1.10) vs exposure-matched SPY +21.1% / +5.1% → **inconclusive, no edge**; 30 gap-through-stop exits (worst −24.1%) |

## 3. Strategy proposals (still PENDING, nothing activated)
| Proposal | Recommendation |
|---|---|
| P-1 signal model as entry filter | **Don't approve yet.** No consistent edge in either period |
| P-2 regime gate | **Don't approve yet.** Helps in-sample, not out-of-sample |
| P-3 universe | Your choice. The list was built for $1,000; at $100k a broader liquid large-cap list is possible. Fine as-is for the dry run |

Review written to `memory/STRATEGY-PROPOSALS.md`.

## 4. Cloud deployment package
- Owner checklist: [CLOUD-DEPLOYMENT.md](CLOUD-DEPLOYMENT.md) covers environment, network allowlist, two network secrets, 5 env vars, 5 tasks with exact crons, connectors removed, and the smoke test.
- Five-day plan: [PHASE4-DRY-RUN-PLAN.md](PHASE4-DRY-RUN-PLAN.md) covers the daily 5-minute check, success criteria and abort criteria.
- **Important finding:** new scheduled tasks include **all** your claude.ai connectors by default. Your account has a trading connector that can place trades or enable trading outside XAHIL. **Remove every connector from every task.**

## 5. What you must do manually before cloud deployment
1. **Fix Telegram.** In Telegram, open **@Veyron_Trade_Bot** and press **Start** (or send "hi"). Run `python -m xahil telegram-setup`, copy the `chat_id` it lists into `.env` as `TELEGRAM_CHAT_ID`, then run `python -m xahil notify "test"` and expect `"sent": true`.
   If `telegram-setup` still shows no chats, another app may be consuming the bot's updates (for example a Telegram plugin using the same bot token). Pause it and retry.
2. **Approve commit and push** of the Phase 4 changes, so the cloud gets the $100k baseline and snapshot.
3. **GitHub:** confirm the repo is **private** and `main` accepts pushes from your account; grant the Claude GitHub app access to only this repo.
4. **Cloud environment:** create it exactly per CLOUD-DEPLOYMENT §2 (allowlist, Alpaca and Tavily network secrets, `XAHIL_CLOUD`, `XAHIL_ALPACA_AUTH=proxy`, `XAHIL_TAVILY_AUTH=proxy`, Telegram variables with the **corrected** chat ID). Never set `XAHIL_ORDER_EXECUTION` or `ALPACA_ENDPOINT`.
5. **Five tasks:** create them paused, prompts pasted verbatim, **no connectors**, Sonnet-class model. Smoke-test pre-market with **Run now** between 06:00 and 09:25 ET on a trading day, then enable the schedules when you're ready.

## 6. Known limitations
- Linux cloud runtime not exercised end to end (only simulated locally); the first Run now is the real test.
- Network secrets carrying **two** Alpaca headers is unverified; the fallback (paper keys as variables) is documented.
- No stop protects against overnight gaps (backtest worst −24%).
- The backtest shows no edge: dry-run validation tests the *system*, not profitability.
