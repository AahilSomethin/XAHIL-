# Weekly Review (PAPER)

Saturday reviews of the week just ended. The Stats and Closed Trades tables
are pasted from `python3 -m xahil weekly-stats` (computed, not estimated).

Template:

## Week ending YYYY-MM-DD (PAPER)

### Stats (computed by `xahil weekly-stats`)
| Metric | Value |
|--------|-------|
| ... | ... |

### Closed Trades
| Ticker | Qty | Entry | Exit | P&L | Return |

### Open Positions at Week End
| Ticker | Entry | Close | Unrealized | Stop |

### Dry-run Proposals This Week (while execution is off)
- What the bot would have done, and how it would have worked out

### What Worked
- ...

### What Didn't Work
- ...

### Key Lessons
- ...

### Recommended Strategy Changes (for owner approval - NOT applied)
- See memory/STRATEGY-PROPOSALS.md

### Overall Grade: X

## Week ending 2026-11-06 (PAPER)

### Stats (computed by `xahil weekly-stats`)
| Metric | Value |
|--------|-------|
| Starting portfolio | $100,000.00 |
| Ending portfolio | $100,000.00 |
| Week return | +$0.00 (+0.00%) |
| S&P 500 (SPY) week | -2.21% |
| Bot vs SPY (week) | +2.21% |
| Week max drawdown | +0.00% |
| Since start | +$0.00 (+0.00%) |
| SPY since start | -2.21% |
| Max drawdown since start | +0.00% |
| Closed trades this week | 0 (W:0 / L:0) |
| Win rate this week | n/a |
| Win rate since start | n/a (0 trades) |
| Best / worst this week | n/a / n/a |
| Profit factor (week) | n/a |

### Closed Trades
| Ticker | Qty | Entry | Exit | P&L | Return |
|---|---|---|---|---|---|
| (none) | | | | | |

### Learning analysis (computed by `xahil learn`, week 2026-11-02 to 2026-11-06)

- Regime days: {'neutral': 3, 'risk_on': 1}
- Signals this week: 268 evaluated, 45 qualified; most common failed checks: [('uptrend', 191), ('relative_strength', 152), ('near_high', 134), ('rsi_pullback', 98), ('volatility', 32)]
- Decisions: {'watch': 6, 'skip': 9, 'propose': 1, 'hold': 1}; evidence items: 107
- Proposals by status: none
- Risk-engine rejections by check: none

#### Signal efficacy (signals from the last 5 weeks with a complete 10-day outcome)
| Group | N | Avg 5d | Avg 10d | 10d hit rate | Stop hit within 10d |
|---|---|---|---|---|---|
| qualified | 0 | n/a | n/a | n/a | n/a |
| rejected | 0 | n/a | n/a | n/a | n/a |
- Qualified minus rejected (10d): n/a (need at least 10 of each)

#### Dry-run proposal outcomes (hypothetical next-open entry, 10% stop)
| Symbol | Proposed | Entry | 5d | 10d | Stop hit (day) |
|---|---|---|---|---|---|
| (none) | | | | | |

#### Mistake candidates
- none detected

### Monthly backtest (memory/backtests/2026-11-07T1508Z.md)
- Assessment: inconclusive. In-sample -8.32% (PF 0.64, 137 trades); out-of-sample trails SPY and exposure-matched SPY (+4.49%); 32 stop exits gapped through the stop.

### Open Positions at Week End
| Ticker | Entry | Close | Unrealized | Stop |
|---|---|---|---|---|
| (none) | | | | |

No positions, so nothing overnight-unprotected. Reconcile clean, no halts.

### Dry-run Proposals This Week (while execution is off)
- None logged as PROPOSED (dry-run). One idea (PFE, 11-04) was skipped after a gap cut R:R to ~1.2:1.

### What Worked
- Stayed in cash through a -2.21% SPY week (+2.21% vs SPY by default).
- Discipline: PFE skipped when R:R degraded; no chasing.
- Reconcile clean all week, no halts, no risk-engine rejections.
- Research evidence logged consistently (107 items).

### What Didn't Work
- Zero entries: no dated catalyst met the bar; no real test of the strategy yet.
- Outperformance came from inaction, not skill; it says nothing about edge.
- Signal efficacy still unmeasurable (0 signals with 10d outcomes).

### Key Lessons
- Cash beat SPY this week only because SPY fell; not evidence of edge.
- Monthly backtest still shows no consistent signal edge; keep signals advisory.
- Need 10+ qualified and 10+ rejected 10d outcomes before judging signals.

### Recommended Strategy Changes (for owner approval - NOT applied)
- See memory/STRATEGY-PROPOSALS.md (no new proposals; 3 existing remain PENDING)

### Overall Grade: B
