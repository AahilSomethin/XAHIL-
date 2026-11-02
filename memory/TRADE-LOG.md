# Trade Log (PAPER)

Every order (or dry-run proposal) and every EOD snapshot. Alpaca is the source
of truth; machine-readable records are in memory/state/*.jsonl.

Entry format:
### YYYY-MM-DD HH:MM ET - BUY|SELL SYM (EXECUTED | PROPOSED (dry-run) | REJECTED)
- Size: qty or $notional | Fill/ref price: $X | Stop: $X | Target: $X | R:R X:1
- Thesis / reason:
- Risk engine: APPROVED or the rejection reasons

## Day 0 - EOD Snapshot (pre-launch baseline)
**Portfolio:** $100,000.00 | **Cash:** $100,000.00 (100%) | **Day P&L:** $0 | **Since start:** $0

No positions yet. Order execution is disabled (dry-run) until the owner approves it.

### 2026-11-02 - EOD Snapshot (PAPER)
**Portfolio:** $100,000.00 | **Cash:** $100,000.00 (100.0%) | **Day P&L:** +$0.00 (+0.00%) | **Since start:** +$0.00 (+0.00%)
**Week to date:** +0.00% vs SPY +0.00% | **Since start vs SPY:** +0.00% | **Max drawdown:** +0.00% | **Buys this week:** 0/3

| Ticker | Shares | Entry | Close | Day Chg | Unrealized P&L | Stop |
|---|---|---|---|---|---|---|
| (no positions) | | | | | | |

**Notes:** No trades or proposals today; no positions, so no stops needed and nothing overnight-unprotected (reconcile clean, no halts). Execution is still in dry-run. Tomorrow: review fresh research (midterms, AMD earnings after close) and only act on a dated catalyst with sufficient evidence; default HOLD.
