# Conformance Audit: XAHIL vs Nate Herk's "Opus 4.7 Trading Bot — Setup Guide"

Audited 2026-10-30, after Phase 3. Principle kept throughout: **Claude Code is the
decision-making agent; Python is the deterministic execution and risk layer.** The guide
used bash wrappers and put its rules in the prompt text. XAHIL keeps the guide's shape and
moves every enforceable rule into tested code.

Legend: **Kept** = as in the guide · **Adapted** = same purpose, safer or updated mechanism ·
**Replaced** = per your requirements · **Added** = not in the guide.

## Part-by-part

| Guide part | Guide design | XAHIL | Status | Where |
|---|---|---|---|---|
| 1. What you're building | Claude is the bot; five cron jobs; stateless runs; Git is memory; hard rules as gates | Same architecture. The "hard rules as gates" the guide promised but didn't implement in code are now a deterministic risk engine | Kept / Adapted | `CLAUDE.md`, `xahil/risk.py` |
| 1. Live trading | "Places real trades" | **Paper only**, fail-closed endpoint, key and account checks | Replaced | `xahil/config.py`, `xahil/alpaca.py` |
| 2. Hard rules | No options; 5–6 positions; 20%/position; 3 trades/week; 75–85% deployed | No options, crypto, leverage, shorts or derivative ETPs; 4 positions; **5%/position, 20% total**; 3 buys/week; cash only | Replaced (your limits) | `config/risk_limits.json`, `xahil/risk.py` |
| 2. 10% trailing stop GTC on every position | Assumed always possible | Whole-share entries only, so every position can hold a GTC stop. Fractional DAY stops never count as overnight protection; entries blocked while any position lacks GTC protection | Adapted (fractional gap closed) | `risk.overnight_protection_gaps`, `cli.protective_order` |
| 2. −7% cut, tighten at +15/+20%, never within 3%, never lower | Prompt instructions | `midday-scan` computes them; `sell --risk-exit` is verified by the engine; `protect` refuses lowering or tight stops | Adapted | `cli.midday_plan`, `cli.cmd_protect` |
| 2. Exit sector after 2 failures; follow sector momentum | Prompt instructions | Still Claude's judgment (strategy doc). Relative strength and regime are advisory inputs | Kept | `memory/TRADING-STRATEGY.md`, `xahil/signals.py` |
| 2. Buy-side gate (positions, weekly count, size, cash, PDT, catalyst, stock-only) | Checklist in the prompt | Deterministic, plus quote freshness, market open, spread, re-check right before submit, day-trade info fail-closed, protection gate | Adapted | `risk._evaluate_buy` |
| 2. Entry checklist (catalyst, sector, stop, 2:1) | Agent documents | Research-log entry with evidence ids; `signals` supplies an explainable stop/target in the 7–10% band at 2:1 | Kept / Added | `routines/pre-market.md` |
| 3. Repository layout | `CLAUDE.md`, `env.template`, `.claude/commands` (7), `routines/` (5), `scripts/` (3), `memory/` (5) | Same, with `scripts/` → `xahil/` Python package; `.env.example`; memory extended | Adapted | repo root |
| 3. Local vs cloud modes | `.env` locally, routine env vars in cloud | Same; `.env` parsed, never executed, ignored in the cloud; network secrets supported | Adapted | `config.load_dotenv`, `routines/README.md` |
| 4. `alpaca.sh` | Raw JSON orders, `close-all`, `cancel-all`, live default | Paper-only client, no raw orders, no bulk liquidation, redirects refused | Replaced | `xahil/alpaca.py` |
| 4. `perplexity.sh` | Required, uncapped, exit 3 → WebSearch | **Tavily free tier is primary** (basic search only, credits reserved before each call, 900/month and 40/day caps, 6-hour cache, HTTP 432 blocks the month). Perplexity optional and **off**. Exit 3 → WebSearch. Every source is logged with URL, publication date and retrieval time. Insufficient research blocks entries (never bullish) | Adapted | `xahil/tavily.py`, `xahil/research.py` |
| 4. `clickup.sh` | ClickUp chat, uncommitted fallback file | **Telegram**, committed outbox fallback, token redaction | Replaced | `xahil/notify.py` |
| 4. Alpaca gotchas (PDT, string qty, data URL, quote shape, UTC) | Notes | Encoded: day-trade count from broker or fills, string formatting, approved data host, quote validity and freshness, ET from the broker clock | Adapted | `xahil/risk.py`, `xahil/cli.py` |
| 5. Pre-market | Perplexity research, ideas, default HOLD | Plus regime, recorded signals (qualified and rejected), evidence log, decision log, `size` + `check-buy` per idea | Kept / Added | `routines/pre-market.md` |
| 5. Market-open | Market buy, then trailing stop; PDT fallback ladder | Protection FIRST, then whole-share buys, each followed by a GTC stop; dry-run until approved | Adapted | `routines/market-open.md` |
| 5. Midday | Cut −7%, tighten, thesis check | Same through `midday-scan`; cancels the stop before selling (the guide had the order reversed); fractional positions flagged | Adapted | `routines/midday.md` |
| 5. Daily summary | Equity from the log, ClickUp | Deterministic snapshot (equity history, SPY, drawdown); refuses to record a bad baseline; reports overnight protection | Adapted | `cli.cmd_snapshot` |
| 5. Weekly review | Friday; S&P via Perplexity; **edits the strategy** | Saturday; computed stats (SPY from Alpaca data); learning analysis; monthly backtest; **proposals only** | Adapted / Replaced | `routines/weekly-review.md`, `xahil/learning.py` |
| 5. Ad-hoc portfolio / trade | Read-only snapshot; y/n then execute | Same snapshot; trade = risk check + human-typed confirmation in your own terminal | Adapted | `.claude/commands/` |
| 6. Memory model (5 files) | Markdown, append-only, committed | Kept, plus JSONL logs (decisions, evidence, signals, regime, mistakes, proposals, order journal), equity history, snapshots, run markers, backtest reports, MISTAKES.md | Kept / Added | `memory/` |
| 6. "Where memory-in-git would break" | Close schedules; manual edits; partial failure → reconcile | Run markers and the Git lock stop duplicates; union merge for logs; order journal and reconcile resolve partial failures; persistent halts for unexplained changes | Adapted (each risk addressed) | `xahil/runs.py`, `xahil/persist.py`, `xahil/reconcile.py` |
| 7. Cloud routines | Routine timezone; env vars; "unrestricted pushes" toggle | `CRON_TZ=` crons; network secrets; Custom network allowlist; `HEAD:main` via `persist` | Adapted (2026 docs) | `routines/README.md` |
| 7. Cron schedule (Central, at the bell and at the close) | 8:30 CT open / 3:00 CT close | 09:47 / 12:07 / 16:22 ET plus Saturday review; windows enforced in code, half-days respected | Adapted | `xahil/runs.py` |
| 8. Prompt scaffold | Persona, env check, persistence warning, steps, commit, rebase, never force | Same 8-part shape, generated from one builder; adds run claim, reconcile and guard; pushes only through `persist` | Kept / Adapted | `routines/_build.py` |
| 9. Troubleshooting | Push and env issues | Persist failures alert; `preflight` diagnoses; fail-closed messages | Adapted | `xahil/cli.py` |
| 10. Replication checklist | Ordered steps | Ordered steps plus `activation-check` | Adapted | `docs/SETUP.md` |
| 11. Notification philosophy | Quiet; always daily and weekly | Kept, plus safety alerts (protection failure, uncertain orders, halts, overnight gaps, persist failure) | Kept / Added | `routines/*.md`, `cli.alert` |
| Appendix A — CLAUDE.md | LIVE, aggressive persona | Paper, disciplined persona; full command table; never-list | Adapted | `CLAUDE.md` |
| Appendices B–E | env template, three scripts | `.env.example`; Python modules | Replaced | — |
| Appendix F — five prompts | Paste verbatim | Paste verbatim; tested against the real CLI | Kept | `routines/` |
| Appendix G — slash commands | 7 commands | 7 commands (local: no push) | Kept | `.claude/commands/` |
| Appendix H — starter memory | 5 files at $10k | 5 files at $1k, plus MISTAKES, STRATEGY-PROPOSALS, RECONCILIATION-LOG, NOTIFICATIONS-OUTBOX | Adapted | `memory/` |

## Added beyond the guide (your Phase 3 request)
| Capability | Deterministic part | Claude's part |
|---|---|---|
| Technical analysis | SMA/EMA/RSI/ATR/ROC/52-week high/realized volatility, lookahead-free (`xahil/ta.py`) | Interprets them in research |
| Explainable signals | 7 named checks with value, threshold and explanation; stop/target (`xahil/signals.py`) | Picks catalysts; signals are advisory |
| Market regime | risk_on / neutral / risk_off / high_volatility from SPY (`xahil/regime.py`) | Defaults to HOLD in risk_off or high volatility |
| Backtesting with out-of-sample | Next-open fills, gap-through-stop fills, live caps, fixed params, IS/OOS split, exposure-matched benchmark (`xahil/backtest.py`) | Reads the report in the weekly review |
| Learning system | Forward outcomes of signals and dry-run proposals, rejection statistics, mistake candidates (`xahil/learning.py`) | Confirms mistakes, writes lessons and proposals |
| Run safety | Calendar windows, duplicate-run lock, interruption detection, safe push (`runs.py`, `persist.py`) | Follows exit codes |

## Deliberate deviations still standing
1. **Weekly strategy edits removed:** the agent proposes; you approve.
2. **Whole shares only:** this closes the fractional overnight gap, but restricts the universe to stocks under about $49 at $1,000 capital.
3. **No PDT fallback ladder:** same-day discretionary round trips are blocked instead; protective exits are always allowed.
4. **Signals and regime are not entry gates:** making them gates is PENDING proposals P-2026-10-30-1 and -2.
