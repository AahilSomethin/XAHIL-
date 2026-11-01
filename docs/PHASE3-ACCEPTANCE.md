# Phase 3 Acceptance: Trading Intelligence and Operational Readiness

> **Superseded baseline (2026-11-01):** this report was written for a $1,000 baseline. The owner kept the $100,000 paper account; blocker "reset to $1,000" no longer applies. See docs/PHASE4-READINESS.md.

| | |
|---|---|
| Date | 2026-10-30, 00:15–01:05 ET (market closed) |
| Code under test | commit `a70d416` + uncommitted Phase 2/3 changes (nothing committed or pushed) |
| Environment | local Windows, Python 3.13.7 and 3.11.9 |
| Order execution | **OFF**: `order_execution_enabled: false`, `XAHIL_ORDER_EXECUTION` unset. No orders sent. No scheduled tasks |
| Credentials / limits | untouched / unchanged (5% · 20% · 3 per week · 4 positions · $1,000) |

## Verdict
- **Implementation:** complete and tested. **234/234 tests pass** on 3.13 (unittest and pytest) and 3.11.
- **Live read-only integration:** passes. Over 300 GET requests to the paper and data APIs across all checks; 0 write attempts.
- **Activation readiness:** **NOT READY.** `activation-check` fails on two blocking items: the $100k balance (B1) and no dry-run track record (B2).
- **Strategy evidence:** the advisory signal model shows **no robust edge** (backtest inconclusive, below). Do not treat it as a reason to activate.

---

## 1. Completed features

### 1.1 Technical analysis, signals, regime, backtesting
| Feature | Implementation | Tests |
|---|---|---|
| Indicators | SMA, EMA, Wilder RSI, Wilder ATR, ROC, 252-day high, 20-day realized volatility. Pure Python, lookahead-free (value at i uses data ≤ i) | `test_analysis.Indicators` incl. `test_no_lookahead` |
| Explainable signals | 7 named checks (history, uptrend, relative strength vs SPY, RSI pullback, near 52-week high, ATR volatility, whole-share affordability), each with value, threshold and plain explanation; stop = entry − 2·ATR clamped to the 7–10% band; target = 2R. **Advisory only** | `test_analysis.Signals` |
| Market regime | risk_on / neutral / risk_off / high_volatility / unknown from SPY SMA50/200 and volatility, with reasons and metrics. **Advisory only** | `test_analysis.Regimes` |
| Backtester | Next-day-open fills plus slippage; overnight gaps through a stop fill **at the open**; trailing-stop tightening; −7% cut at the next open; live caps (5%/20%/3 per week/4 positions), cash only, whole shares; fixed parameters | `test_analysis.Backtests` (caps, gap fill, no lookahead) |
| Out-of-sample evaluation | Chronological 70/30 split; out-of-sample starts with fresh $1,000; verdict with flags (few trades, negative, below SPY, below **exposure-matched** SPY, PF < 1, decay, gap exits, **inconsistent in-sample**) | `test_out_of_sample_split`, `test_assessment_flags` |

### 1.2 Git-backed memory and learning
| Log (memory/state/) | Written by | Content |
|---|---|---|
| `decisions.jsonl` | `decide` | propose / watch / skip / hold / exit with reason and evidence ids (unknown ids flagged) |
| `evidence.jsonl` | `evidence`, `research --log` | claim, source URL (validated), kind, marked untrusted-external |
| `signals.jsonl` | `signals --record` | **every** evaluation, qualified and rejected, with failed checks |
| `regime_history.jsonl` | `regime --record` | daily regime and metrics |
| `mistakes.jsonl` + `MISTAKES.md` | `mistake` | what happened, lesson |
| `proposed_orders.jsonl` / `executed_orders.jsonl` | order commands | dry-run proposals, rejections, order journal |
| `backtests.jsonl` + `memory/backtests/*.md` | `backtest --save` | summary and full report |
| `runs/<date>/<workflow>.json` | `run-start` / `run-finish` | run markers |

`learn` (weekly): regime days; signal efficacy (5- and 10-day forward returns and stop-hit rate,
qualified vs rejected, edge reported only with ≥10 per group); dry-run proposal outcomes;
risk-engine rejections by rule; decisions without evidence; **mistake candidates** (loss beyond the
stop band, stopped out within 3 days, ≥3 rejections on one rule, proposals hitting their stop
within 5 days). It recommends only: proposals go to STRATEGY-PROPOSALS as PENDING.

### 1.3 Telegram, Perplexity, routines
- **Telegram:** plain-text sends, 4,000-character cap, committed outbox fallback, token redaction. New safety alerts: protection failures, overnight gaps, uncertain orders, new halts, persist failures.
- **Perplexity:** off by default, monthly cap, `--log` stores citations as evidence; exit 3 falls back to WebSearch plus `evidence`.
- **Routines:** all five regenerated from `routines/_build.py`. Tests confirm every command and flag in the prompts exists in the CLI, steps run in a safe order (run-start → persist marker → preflight → reconcile … run-finish → guard → persist), there is no raw `git push`, no `--notional`, and market-open protects before buying.

### 1.4 Run safety
| Concern | Mechanism | Tests |
|---|---|---|
| Market calendar | `run-start` checks Alpaca's calendar: holidays → exit 5; per-workflow ET windows derived from the session (half-day midday ends 12:30); weekly review only at weekends, once per ISO week | `Runs.test_calendar_awareness`, `test_weekly_review_once_per_week…` |
| Duplicate runs | Marker per (date, workflow): completed → skip; running < 90 min → skip; marker pushed first so a concurrent run's push collides and stops | `test_window_proceed_duplicate_and_finish`, `test_duplicate_run_marker_conflict_stops_second_run` |
| Interruption | Stale marker → recovery with a note; other unfinished workflows reported; order journal resolved by reconcile (Phase 2) | `test_interrupted_run_recovered…`, `test_other_interrupted_workflows_reported` |
| Git persistence | `persist`: guard (memory/ only) → commit → push → on rejection `pull --rebase` → union merge for append-only logs (`.gitattributes`) → any other conflict aborts the rebase and fails loudly → verifies origin contains the commit. **Never force-pushes** | `Persistence` (temporary bare repos: push, no-op, guard block, union merge, marker conflict) |

### 1.5 Fractional-share overnight protection gap: closed by design
1. **Entries must be whole shares.** The risk engine rejects `--notional` and fractional quantities (`gtc_protectable`).
2. **Only a GTC stop or trailing stop on whole shares counts as overnight protection.** A DAY stop (the only option for fractional shares at Alpaca) never counts.
3. **Any position without GTC protection blocks all new entries at all hours.** Reconcile reports `overnight_unprotected`. Daily-summary, pre-market and weekly reconciles alert. `midday-scan` flags fractional positions for an exit decision. `activation-check` is NOT READY while any exist.
4. **Honest limit:** no stop prevents an overnight gap. The live backtest shows it: the worst trade was **−22.8% against a 10% stop**, with 16 out-of-sample stop exits gapping through. Stops cap ordinary losses, not gap losses.
5. **Consequence at $1,000:** the $50 position cap means only stocks under about $49 are buyable (for example F fits 3 shares; SPY fits none).

Tests: `test_overnight_protection.py` (11) plus updated suites.

### 1.6 Guide conformance
See [GUIDE-CONFORMANCE.md](GUIDE-CONFORMANCE.md): every part of the guide is mapped as kept, adapted, replaced or added. Claude remains the decision-maker; every enforceable rule is in tested Python.

---

## 2. Test results (actually run)
| Suite | Tests |
|---|---|
| test_analysis | 21 |
| test_cli_execution | 25 |
| test_integrations_guard | 21 |
| test_memory_runs_persist | 28 |
| test_metrics_reconcile | 11 |
| test_overnight_protection | 11 |
| test_paper_guard | 19 |
| test_phase3_readiness | 58 |
| test_risk | 35 |
| test_routines | 5 |
| **Total** | **234 passed** (3.13 unittest; 3.13 pytest: 234 passed, 212 subtests; 3.11 unittest) |

Failures found and fixed during this run:
- My test helper shadowed `TestCase.run`.
- The backtest recorded trades only on close. That looked like lookahead at first; it wasn't, and trades are now recorded at entry.
- Fixtures predated the new fail-closed rules.
- Windows line-ending conversion in the temporary Git repos.
- A routine test was too literal.
- The backtest verdict ignored a failing in-sample period.
- `signals --explain` re-downloaded the whole universe.
- MRO was delisted and has been removed from the universe.

## 3. Live read-only integration (real Alpaca paper and data APIs)
All writes went to a scratch folder; non-GET requests were blocked (0 attempted); Telegram alerts were captured, not sent.

| Check | Result |
|---|---|
| `preflight` | PASS. Paper endpoint, `PA` account, dry-run, quote limit 60 s, Telegram configured |
| `market-status` | PASS. 2026-10-30 trading day, closed at check time |
| `regime` | **risk_on**: SPY 769.49 > SMA50 766.86 > SMA200 725.60, 20-day volatility 10.1% |
| `signals` (68 symbols) | **4 qualified**: HPQ, KDP, KMI, PFE (stops 7.0–7.9%, 2:1 targets); near misses explained (e.g. KO/MO/OXY fail whole-share affordability) |
| `signals --explain F` | all 7 checks with values (fails uptrend, relative strength, near high) |
| `size F` / `size SPY` | 3 shares ($39.57) / 0 shares, refused |
| `check-buy F --qty 1` | correctly REJECTED: $100k halt, market closed, quote 31,977 s old, after-hours spread 9.8%, no research entry |
| `reconcile` | correct HALT ($100k); day trades computed from fills; no overnight gaps |
| `run-start pre-market` at 00:58 ET | exit 5, outside window. `weekly-review` on a weekday: exit 5 |
| `learn` | runs; no history yet, so the analysis is empty |
| `activation-check` | NOT READY: baseline equity, dry-run track record |
| `persist --dry-run` | correctly refused (code changes outside memory/ are uncommitted) |
| `backtest` (×2) | below |

### Backtest on real IEX history (68 symbols, Nov 2018 – Oct 2026, fixed parameters)
| Window | Return | Max DD | SPY | Exposure-matched SPY | Trades | Win % | PF | Avg exposure |
|---|---|---|---|---|---|---|---|---|
| In-sample 2018-11-01 → 2024-12-09 | **−2.82%** | −7.24% | +145.3% | +17.95% | 124 | 34.7 | **0.77** | 12.4% |
| Out-of-sample 2024-12-10 → 2026-10-29 | **+10.12%** | −3.18% | +30.6% | +4.35% | 71 | 42.3 | 2.11 | 14.2% |
| OOS with regime filter | +12.03% | −3.72% | +30.6% | +4.32% | 72 | 45.8 | 2.32 | 14.1% |

**Verdict: inconclusive.** Good results out-of-sample but losses in-sample, so the edge isn't
consistent; it trails SPY in both windows. The regime filter barely changes anything. Caveats:
survivorship bias (today's universe), the IEX feed, and no catalysts modelled (the live strategy
requires one). Reports are in `memory/backtests/2026-10-30-*.md`.

---

## 4. Remaining blockers
| # | Blocker | Owner action |
|---|---|---|
| B1 | Paper account equity is **$100,000**: all entries halted; `activation-check` baseline fails | Reset the paper account to **$1,000** before the first real `reconcile` (otherwise run `reset-baseline` in your terminal) |
| B2 | No dry-run track record (0 of 5 daily summaries, 0 of 1 weekly review) | Run ≥5 trading days and 1 weekly review in dry-run (local slash commands or cloud tasks with execution off) |
| B3 | Strategy decisions PENDING: signal filter, regime gate, universe (`memory/STRATEGY-PROPOSALS.md`) | Approve or reject. Nothing is activated without you |

## 5. Needs manual verification
| # | What | How |
|---|---|---|
| M1 | **Telegram delivery** (not sent: messages leave your account and weren't requested this phase) | `python -m xahil notify "XAHIL integration test"` → `"sent": true` |
| M2 | **In-session data:** quote freshness and spread on IEX during market hours | On a trading day around 09:50 ET: `python -m xahil check-buy F --qty 1` |
| M3 | **Clean baseline** after B1 | `preflight` (equity 1000), `reconcile --workflow smoke-test` (exit 0) |
| M4 | **Cloud runtime:** network allowlist, network secrets, `persist` push to GitHub, `run-start` windows in CRON_TZ | First cloud **Run now** inside the pre-market window |
| M5 | **Live order paths** (GTC stop placement, cancel-then-sell, uncertain-response lookup) | Only after you approve execution; covered offline only |

## 6. Known limitations
- **Gap risk** is real and unavoidable with stops (worst backtest trade −22.8%).
- **Whole-share rule** narrows the universe to stocks under about $49 at $1,000.
- **Persistent halts and memory files** live in Git, where an agent run could in principle edit them; the guard limits commits to memory/, and paper-only keys remain the backstop.
- **Duplicate-run lock** depends on the marker push. If GitHub is unreachable, `persist` fails and the run stops (safe, but the run is lost).
- **Signals** rely on IEX bars and a proposed universe with survivorship bias.
- A position change can show up seconds before its fill appears in Alpaca's activity feed, which can raise a false "unexplained change" halt (fails safe, cleared with `ack-halt`).

## 7. Readiness for activation
| Stage | Ready? |
|---|---|
| Local dry-run workflows | **Yes** after B1 |
| Cloud scheduled tasks in dry-run | **Yes** after B1 and M4 |
| Unattended paper order execution | **No.** Needs B1, B2, B3, M1–M4, `activation-check` READY, and your explicit approval (both switches set by you) |
