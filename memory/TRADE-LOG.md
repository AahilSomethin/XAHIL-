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

### 2026-11-03 - EOD Snapshot (PAPER)
**Portfolio:** $100,000.00 | **Cash:** $100,000.00 (100.0%) | **Day P&L:** +$0.00 (+0.00%) | **Since start:** +$0.00 (+0.00%)
**Week to date:** +0.00% vs SPY +0.31% | **Since start vs SPY:** +0.31% | **Max drawdown:** +0.00% | **Buys this week:** 0/3

| Ticker | Shares | Entry | Close | Day Chg | Unrealized P&L | Stop |
|---|---|---|---|---|---|---|
| (no positions) | | | | | | |

**Notes:** No trades or proposals today (HOLD: PFE sufficient but binary earnings gap risk on midterms day; KMI/WFC insufficient research). No positions, so no stops needed and nothing overnight-unprotected (reconcile clean, no halts). Execution is still in dry-run. Tomorrow: review post-earnings reaction (PFE, AMD) and midterm results; act only on a dated catalyst with sufficient evidence, default HOLD.

### 2026-11-04 09:50 ET - BUY PFE (REJECTED - agent skip, not risk engine)
- Size: 0 (160 sh max) | Ref price: $30.54 ask | Stop: n/a | Target: $34.18 | R:R ~1.2:1
- Thesis / reason: Q3 beat + raised guidance (Nov 3) [e-2026-11-04-16, -19, -20]; gapped +1.9% above $29.98 ref, R:R fell from 2:1 to ~1.2:1. HOLD.
- Risk engine: check-buy APPROVED (not submitted)

### 2026-11-04 - EOD Snapshot (PAPER)
**Portfolio:** $100,000.00 | **Cash:** $100,000.00 (100.0%) | **Day P&L:** +$0.00 (+0.00%) | **Since start:** +$0.00 (+0.00%)
**Week to date:** +0.00% vs SPY +1.95% | **Since start vs SPY:** +1.95% | **Max drawdown:** +0.00% | **Buys this week:** 0/3

| Ticker | Shares | Entry | Close | Day Chg | Unrealized P&L | Stop |
|---|---|---|---|---|---|---|
| (no positions) | | | | | | |

**Notes:** No trades today; one skipped idea (PFE gapped above the reference price, R:R fell to ~1.2:1, so HOLD; check-buy approved but nothing submitted). No positions, so no stops needed and nothing overnight-unprotected (reconcile clean, no halts). Execution is still in dry-run. Tomorrow: review fresh research and act only on a dated catalyst with sufficient evidence; default HOLD.

### 2026-11-05 - EOD Snapshot (PAPER)
**Portfolio:** $100,000.00 | **Cash:** $100,000.00 (100.0%) | **Day P&L:** +$0.00 (+0.00%) | **Since start:** +$0.00 (+0.00%)
**Week to date:** +0.00% vs SPY -2.47% | **Since start vs SPY:** +2.47% | **Max drawdown:** +0.00% | **Buys this week:** 0/3

| Ticker | Shares | Entry | Close | Day Chg | Unrealized P&L | Stop |
|---|---|---|---|---|---|---|
| (no positions) | | | | | | |

**Notes:** No trades or proposals today (HOLD: BMY sufficient research but no dated catalyst; PFE/WFC insufficient). No positions, so no stops needed and nothing overnight-unprotected (reconcile clean, no halts). Execution is still in dry-run. Tomorrow: review fresh research and act only on a dated catalyst with sufficient evidence; default HOLD.
