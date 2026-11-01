# Strategy Proposals (awaiting owner approval)

The agent never changes strategy rules. It records recommendations here. The
owner approves by editing memory/TRADING-STRATEGY.md and/or
config/strategy_params.json by hand, then marking the proposal APPROVED or REJECTED.

Template:

## P-YYYY-MM-DD-N: <short title>
- Status: PENDING
- Rule affected:
- Proposed change:
- Evidence (weeks observed, trades, numbers):
- Expected effect and risk:
- Owner decision:

## P-2026-10-30-1: Use the signal model as an entry filter
- Status: PENDING (owner decision)
- Rule affected: entry checklist
- Proposed change: require `signals` to show QUALIFIED for a symbol before a buy, in addition to the catalyst.
- Evidence: none yet. Needs dry-run history: at least 10 qualified and 10 rejected signals with 10-day outcomes in `learn`, plus a saved out-of-sample backtest.
- Expected effect and risk: fewer, trend-aligned entries; risk of missing catalyst-driven moves.
- Owner decision:

## P-2026-10-30-2: Use the market regime as an entry gate
- Status: PENDING (owner decision)
- Rule affected: Strategy Rule 2 (sector momentum) / Rule 10 (patience)
- Proposed change: no new entries while `regime` is risk_off or high_volatility.
- Evidence: compare `backtest` with and without `--regime-filter` (out-of-sample).
- Expected effect and risk: smaller drawdowns in sell-offs; fewer trades in choppy markets.
- Owner decision:

## P-2026-10-30-3: Approve the research universe
- Status: PENDING (owner decision)
- Rule affected: which symbols `signals` scans (config/universe.json, status "proposed")
- Proposed change: approve, edit or replace the 68-symbol list.
- Evidence: the list was chosen for a $1,000 account (names under about $49/share). At $100,000 the 5% cap is about $5,000, so price no longer constrains the universe.
- Owner decision:

## Review 2026-11-01 (Phase 4 prep; all three remain PENDING, nothing activated)
Evidence: backtests at the $100,000 baseline, 68 symbols, Nov 2018 - Oct 2026, fixed parameters
(memory/backtests/2026-11-01T0231Z*.md).

| Window | No filter | With regime filter | Exposure-matched SPY |
|---|---|---|---|
| In-sample 2018-11 → 2024-12 | -8.38% (PF 0.61, 136 trades) | -5.11% (PF 0.67, 110 trades) | +21.1% / +17.9% |
| Out-of-sample 2024-12 → 2026-10 | +2.87% (PF 1.10, 103 trades) | +2.23% (PF 1.08, 90 trades) | +5.1% / +4.9% |

- **P-2026-10-30-1 (signal model as entry filter): recommend NOT approving yet.** The signal model has no
  consistent edge: it loses in-sample and trails exposure-matched SPY out-of-sample. Keep it advisory and
  revisit after `learn` has at least 10 qualified and 10 rejected signals with 10-day outcomes.
- **P-2026-10-30-2 (regime gate): recommend NOT approving yet.** It reduces in-sample losses (-5.1% vs -8.4%)
  but does not help out-of-sample. The difference is within noise.
- **P-2026-10-30-3 (universe): owner choice.** The 68-symbol list was picked for a $1,000 account. At
  $100,000 the 5% cap (~$5,000) fits almost any stock, so you may want a broader list of liquid large caps.
  The current list is fine for dry-run validation.
- Gap risk is material at any size: 30 out-of-sample stop exits gapped through the stop (worst -24.1%).
