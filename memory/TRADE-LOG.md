# Trade Log (PAPER)

Every order (or dry-run proposal) and every EOD snapshot. Alpaca is the source
of truth; machine-readable records are in memory/state/*.jsonl.

Entry format:
### YYYY-MM-DD HH:MM ET - BUY|SELL SYM (EXECUTED | PROPOSED (dry-run) | REJECTED)
- Size: qty or $notional | Fill/ref price: $X | Stop: $X | Target: $X | R:R X:1
- Thesis / reason:
- Risk engine: APPROVED or the rejection reasons

## Day 0 - EOD Snapshot (pre-launch baseline)
**Portfolio:** $1,000.00 | **Cash:** $1,000.00 (100%) | **Day P&L:** $0 | **Since start:** $0

No positions yet. Order execution is disabled (dry-run) until the owner approves it.
