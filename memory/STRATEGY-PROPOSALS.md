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
- Evidence: with $1,000 and a 5% cap, only names under about $49/share are buyable as whole shares.
- Owner decision:
