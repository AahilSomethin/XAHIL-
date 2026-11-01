# Phase 2 Acceptance: Alpaca Paper Integration, Telegram Validation, Phase 3 Readiness

> **Superseded baseline (2026-11-01):** this report was written for a $1,000 baseline. The owner kept the $100,000 paper account; blocker "reset to $1,000" no longer applies. See docs/PHASE4-READINESS.md.

| | |
|---|---|
| First run | 2026-10-29 23:49–23:55 ET (market closed) |
| Readiness fixes and re-run | 2026-10-30 00:00–00:15 ET (market closed) |
| Code under test | commit `a70d416` + uncommitted changes listed below |
| Environment | local Windows, Python 3.13.7 and 3.11.9, credentials from the local `.env` |
| Order execution | **disabled** (both switches off), no orders sent, no scheduled tasks |

**Verdict:** the code is ready for Phase 3 dry-run days. Blocked on account setup (B1)
and three checks that need market hours or your own setup (see "Needs manual verification").

## Rules followed
- No key or secret was printed, logged or written to a tracked file. Only variable *names* were inspected.
- Only `paper-api.alpaca.markets` and `data.alpaca.markets` were called, with GET only. A wrapper blocked
  and counted any other method: zero attempts in both runs.
- No orders were submitted, cancelled or modified. No scheduled tasks were created. No subscription changes.
- Commands that write state were pointed at a scratch folder, so `memory/` is unchanged.
- Approved limits are unchanged. One new limit was added at your request: `max_quote_age_seconds: 60`.

---

## Part A: Phase 2 integration results (2026-10-29)

### Pre-change audit fixes
| # | Finding | Fix | Test |
|---|---|---|---|
| A1 | Network failures crashed with a raw traceback | Converted into a clean `AlpacaError` (exit 1) | `test_network_failure_becomes_clean_alpaca_error` |
| A2 | The credentials object's `repr` showed the key values | Key fields are hidden from `repr` | `test_credentials_never_appear_in_repr` |
| A3 | `snapshot` would record the $100k default as the baseline | Refuses while halted | `test_snapshot_refuses_bad_baseline` |
| A4 | Perplexity network errors crashed with a traceback | Become `ResearchUnavailable` (exit 3) | fallback path |

### Live read-only checks (real paper API)
| Check | Result |
|---|---|
| `preflight` | PASS. Paper endpoint, `PA` account, ACTIVE, dry-run |
| `market-status` and calendar | PASS. Also detects the 2026-11-27 13:00 early close |
| `account` | PASS. Equity and cash $100,000 (B1); buying power $400,000; multiplier 4; shorting enabled |
| `positions`, `orders`, `orders all` | PASS. All empty |
| `quote`, daily bars, asset lookup, fills | PASS |
| `reconcile` | PASS (correct HALT on $100k) |
| `check-buy AAPL` / `check-buy TQQQ` | PASS (correctly rejected) |
| `midday-scan`, `snapshot` (refused), `weekly-stats` | PASS |

### Credential failures (all exit 1, clean message, no secret in output)
Missing keys · invalid key (real HTTP 401) · real key ID with a wrong secret (HTTP 401) · live-format key ·
live endpoint · endpoint with a trailing slash · unknown auth mode. Alpaca keys don't expire on a timer as
far as I know; a revoked key gets the same 401.

### Telegram (2026-10-29)
`notify "XAHIL integration test"` fell back to the outbox, because the variables were empty at the time.
A fake token against the real Telegram API got HTTP 401: it fell back cleanly and the token did not leak.

---

## Part B: Phase 3 readiness fixes (2026-10-30)

| # | Requirement | What changed | Key tests |
|---|---|---|---|
| R1 | Quote freshness | New `max_quote_age_seconds` (60 s; config may set (0, 300], hard cap 300). Every buy needs a quote timestamp no older than the limit, measured against **Alpaca's clock**. Missing, unparseable, timezone-less or future (>5 s) timestamps are rejected. A buy also needs `market_open` true from the broker clock | `QuoteFreshness`, `MarketClosed`, `test_stale_quote_blocks_check_buy` |
| R2 | Day-trade info fails closed; no discretionary same-day round trips | The day-trade count is the broker field, or **computed from Alpaca's own fills** over the last 10 calendar days when the broker returns null (it does on your account). The larger value wins if both exist. If neither is available, all entries are refused. A PDT flag halts entries. Every sell now has a purpose. **Protective** stops are always allowed. **risk_exit** (`sell --risk-exit`) is allowed same-day only if the engine confirms unrealized P&L ≤ −7%. **Discretionary** sells are refused when the shares were bought today, or when that can't be determined | `DayTrades`, `SameDayRoundTrips`, `test_fills_unavailable_blocks_entries_but_not_risk_exits`, `test_same_day_buy_from_fills_blocks_discretionary_sell` |
| R3 | Cash-only, long-only regardless of margin | Spendable cash = min(`cash`, `non_marginable_buying_power`) minus open buys. `buying_power` and the multiplier are never used. New halts: any short market value, long market value above equity (leverage in use), PDT flag. **Every order is re-evaluated on freshly fetched broker state immediately before submission** | `CashOnly`, `test_margin_account_cannot_buy_beyond_cash`, `test_reverification_catches_state_change_before_submit` |
| R4 | Missing or rejected protection blocks entries and alerts | During market hours, any held position without an open stop blocks every new buy. Telegram alerts fire when: a protective order is rejected by the engine or the broker (including an async `rejected` status); a stop can't be cancelled before an exit; a buy is blocked by a gap; or a reconcile (other than market-open, where gaps are expected before stops are re-placed) finds a gap. The market-open prompt now protects positions **before** any buy, protects each new buy immediately, and stops buying if protection fails | `ProtectionGaps`, `test_unprotected_position_blocks_entry_and_alerts`, `test_broker_rejected_protective_order_alerts`, `test_async_broker_rejection_of_stop_alerts`, `test_failed_cancel_stops_and_alerts`, `test_midday_reconcile_alerts_on_gap` |
| R5 | Fractional limits explicit | `protect` chooses only order types Alpaca supports. Fractional: **DAY stop** (no GTC, no trailing), with a printed note that it expires at the close. Whole shares: GTC trailing or GTC stop. The engine rejects GTC or trailing orders on any fractional holding, including a whole-number qty taken from a fractional position. A fractional or notional buy on a non-fractionable asset is rejected. The EOD table marks expired fractional stops | `Fractional` |
| R6 | Tests: stale data, closures, duplicate fills, uncertain responses, resets | See the next table | 58 new tests |
| R7 | Interrupted runs, reconciliation recovery | Order journal: "submitting" is written **before** each request and a final status after. Ambiguous responses (network error, 5xx, 429, duplicate-ID 422) are resolved by looking the order up by its `client_order_id`: found means recovered, not found means **uncertain**, which blocks entries. Reconcile resolves uncertain or interrupted entries, records XAHIL orders missing from the journal, and flags orders not placed by XAHIL. Position or equity changes not explained by fills become a **persistent halt** that only a human can clear (`ack-halt` / `reset-baseline` need a local interactive terminal and are refused in the cloud). Snapshot writes are atomic; a corrupt journal or snapshot halts | `test_interrupted_after_submit_is_recovered_by_reconcile`, `test_unlogged_broker_orders_recovered_and_foreign_flagged`, `test_interrupted_between_cancel_and_sell_is_caught`, `test_corrupt_journal_halts`, `test_account_reset_detected_and_requires_human_rebaseline` |
| R8 | Switches stay off | Unchanged: `order_execution_enabled: false`, `XAHIL_ORDER_EXECUTION` unset | `test_repo_switches_still_disabled`, preflight |

### Scenario coverage added (R6/R7)
| Scenario | Expected behaviour | Result |
|---|---|---|
| Stale quote (61 s, 5 min, after-hours), missing, garbage, naive, future timestamp | Entry rejected | PASS |
| Market closed / clock unknown / holiday / half-day | Entry rejected; `trading_day` false; `early_close` true | PASS |
| Same fill delivered twice | Counted once (metrics, day-trade count, reconcile) | PASS |
| Timeout after Alpaca accepted the order | Looked up by ID, recorded `recovered_submitted`, exactly 1 order | PASS |
| Timeout before acceptance / HTTP 503 | `uncertain`, exit 2, alert, entries blocked; reconcile marks `not_placed`, entries re-allowed | PASS |
| Same buy sent twice (re-run) | Alpaca duplicate-ID rejection, recovered; still exactly 1 order | PASS |
| Definitive broker rejection of an entry | `rejected_by_broker`, no protection alert | PASS |
| State changes between check and submit | `rejected_on_reverify`, nothing sent | PASS |
| Run died after submitting (journal says "submitting") | Reconcile finds the order and recovers the journal | PASS |
| Run died after cancelling a stop, before selling | Next reconcile alerts; entries blocked | PASS |
| XAHIL order at Alpaca missing from the journal / manual dashboard order | Recovered / flagged "not placed by XAHIL" | PASS |
| Paper account reset ($100k → $1k, no fills) | Persistent halt, alert; snapshot refused; agent can't clear it; human `reset-baseline` archives history and clears it | PASS |
| Account number changed | Persistent halt; old snapshot kept | PASS |
| Position vanished without a fill / closed by a stop fill | Halt / explained, no halt | PASS |
| `reset-baseline` in the cloud | Refused | PASS |

### Live re-run on the real paper API (2026-10-30 00:10 ET, read-only, scratch paths)
| Check | Result |
|---|---|
| `preflight` | PASS. Now also shows quote-age limit 60 s, multiplier 4, shorting enabled, no persistent halts. Telegram: **configured** (both variables now set) |
| `reconcile` ×2 | PASS. Correct HALT on $100k. Day-trade count 0, **source: computed_from_fills** (broker field null). Second run compared against the first snapshot with no unexplained changes |
| `check-buy SPY --notional 40` | PASS (correctly rejected): halt, `market_open` false, **quote 26,613 s old (limit 60 s)**, no research entry for the new date |
| `halts`, `midday-scan`, `snapshot` | PASS. No halts or actions; snapshot refused |
| Non-GET attempts / alerts | 0 / 0 |

### Automated tests
| Run | Result |
|---|---|
| `python -m unittest discover -s tests -t .` (3.13.7) | **169 passed** |
| `python -m pytest -q` (3.13.7) | **169 passed**, 83 subtests |
| `py -3.11 -m unittest discover -s tests -t .` | **169 passed** |

---

## Blockers for Phase 3

| # | Blocker | Action (yours) |
|---|---|---|
| B1 | Paper equity is still **$100,000**. Every entry is blocked | Reset the paper account to **$1,000** in the Alpaca dashboard. Do it **before** the first real (non-scratch) `reconcile`, so the first snapshot is the $1,000 baseline. If a snapshot exists from before the reset, run `python -m xahil reset-baseline` in your own terminal |

## Needs manual verification
| # | What | How |
|---|---|---|
| M1 | **Telegram delivery.** Variables are set, but no real message has been sent | `python -m xahil notify "XAHIL integration test"`: expect `"sent": true` and the message in your chat |
| M2 | **In-session behaviour with IEX data.** Spreads and quote freshness during market hours are untested; IEX sees a thin book and quotes may update less than once a minute on quiet names | On a trading day around 09:50 ET: `python -m xahil check-buy SPY --notional 40`. Check `quote_fresh` and `spread` (the halt will still fail until B1). Repeat for one or two small-cap ideas |
| M3 | **Clean baseline after reset** | After B1: `python -m xahil preflight` (equity 1000) and `python -m xahil reconcile --workflow smoke-test` (expect exit 0). Note: this writes the first real snapshot to `memory/state/` |
| M4 | **Human-only commands** | Optional: `python -m xahil halts`; if any halt appears, try `ack-halt --all` in your terminal (it asks you to type ACK) |
| M5 | **Live order paths** (stop placement, uncertain-response lookup, cancel-then-sell) | Only after you approve execution. They're covered by offline tests only |

## Unresolved issues (not blockers)
| # | Issue | Status / recommendation |
|---|---|---|
| U1 | No quote-age check | **Fixed** (R1) |
| U2 | Account allows 4× margin and shorting | **Hardened in code** (R3). Optional: also set multiplier 1 and disable shorting in Alpaca's paper settings |
| U3 | `daytrade_count` null | **Fixed** (R2): computed from fills; refuses entries when unknown |
| U4 | IEX-only data may cause frequent spread or freshness rejections | Open. Verify in M2. A tighter or looser setting is your call (within the 300 s hard cap) |
| U5 | Not testable yet: live order paths, real positions and fills, cloud network secrets | Open. Phase 3 dry-run days, then execution approval |
| U6 | Paper keys were pasted in chat earlier | Optional: regenerate |
| U7 | Fractional positions are **unprotected overnight**. DAY stops expire at the close and gaps can open past any stop | Inherent to Alpaca fractional rules at a $50 position size. Alternative: whole-share positions only (GTC stops), which limits you to stocks under about $50 |
| U8 | Persistent halts live in `memory/state/halts.json`, which an agent run could in principle edit | Defence-in-depth only. Review commits touching `memory/state/halts.json`. Paper-only keys remain the backstop |
| U9 | A position change can be logged a few seconds before its fill appears in Alpaca's activity feed, which would raise a false "unexplained" halt | Fails safe: a human acknowledges it with `ack-halt` |

## Readiness for Phase 3
- **Code:** ready for dry-run trading days.
- **Account:** not ready until B1 is done.
- **Recommended order:** B1 → M3 → M1 → a week of local dry-run workflows (M2 on day one) → review proposals → decide on execution approval.
