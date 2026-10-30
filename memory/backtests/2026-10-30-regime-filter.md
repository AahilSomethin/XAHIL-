# Backtest 2026-10-30T0503Z (PAPER research, advisory signal model)

- Universe (68): F, T, PFE, INTC, BAC, VZ, CMCSA, KMI, CSCO, KO, KHC, WBD, GM, CCL, AAL, DAL, UAL, SOFI, SNAP, PINS, LYFT, RIVN, NU, HPE, HPQ, NOK, ERIC, VALE, PBR, KGC, AA, FCX, CLF, HAL, SLB, DVN, OXY, BMY, CVS, KDP, MO, PCG, AES, PPL, NEE, USB, WFC, C, SCHW, SCHD, SCHX, SCHB, SCHF, SCHE, XLF, XLU, XLB, XLP, KRE, EEM, EWZ, FXI, GDX, SLV, IEFA, IEMG, VEA, VWO
- Regime filter: on (no entries in risk_off / high_volatility)
- Split date (out-of-sample starts): 2024-12-10
- Fixed parameters from config/signal_params.json and config/strategy_params.json (no fitting).
- Execution: next-day open + slippage; gaps through stops fill at the open; whole shares; caps as live.
- Caveats: survivorship bias (today's universe), IEX-feed bars, no catalysts/thesis exits modelled.

## Assessment: inconclusive
- out-of-sample return trails the S&P 500 (SPY)
- 15 stop exits gapped through the stop at the open
- in-sample period fails (return -2.23%, profit factor 0.77): results are not consistent across periods

## In-sample
| Metric | Value |
|---|---|
| start | 2018-11-01 |
| end | 2024-12-09 |
| trading_days | 1102 |
| end_equity | 977.66 |
| total_return_pct | -2.23 |
| cagr_pct | -0.37 |
| max_drawdown_pct | -6.53 |
| benchmark_return_pct | 145.27 |
| excess_vs_benchmark_pct | -147.5 |
| exposure_matched_benchmark_pct | 15.33 |
| closed_trades | 104 |
| win_rate_pct | 34.6 |
| profit_factor | 0.77 |
| avg_trade_return_pct | -0.99 |
| avg_hold_days | 36.4 |
| avg_exposure_pct | 10.55 |
| gap_through_stop_exits | 19 |
| worst_trade_pct | -22.84 |
| rejected_entries | {'weekly buy cap': 110, 'max positions': 4305, 'no bar on entry day': 12} |

## Out-of-sample
| Metric | Value |
|---|---|
| start | 2024-12-10 |
| end | 2026-10-29 |
| trading_days | 473 |
| end_equity | 1120.3 |
| total_return_pct | 12.03 |
| cagr_pct | 6.22 |
| max_drawdown_pct | -3.72 |
| benchmark_return_pct | 30.58 |
| excess_vs_benchmark_pct | -18.55 |
| exposure_matched_benchmark_pct | 4.32 |
| closed_trades | 72 |
| win_rate_pct | 45.8 |
| profit_factor | 2.32 |
| avg_trade_return_pct | 3.8 |
| avg_hold_days | 30.5 |
| avg_exposure_pct | 14.13 |
| gap_through_stop_exits | 15 |
| worst_trade_pct | -12.87 |
| rejected_entries | {'weekly buy cap': 109, 'max positions': 2941} |

## Out-of-sample trades
| Symbol | Entry | Exit | Shares | Return | Reason |
|---|---|---|---|---|---|
| SOFI | 2024-12-11 $15.46 | 2024-12-18 $15.39 | 3 | -0.44% | stop |
| CCL | 2024-12-11 $25.55 | 2024-12-30 $24.02 | 1 | -5.99% | stop |
| HPE | 2024-12-11 $20.85 | 2025-01-28 $21.34 | 2 | +2.33% | stop |
| AAL | 2024-12-16 $16.95 | 2025-01-23 $17.17 | 2 | +1.28% | stop |
| KMI | 2024-12-20 $24.36 | 2025-01-27 $27.00 | 2 | +10.83% | stop (gapped through at open) |
| CCL | 2024-12-31 $24.60 | 2025-02-11 $25.28 | 2 | +2.76% | stop (gapped through at open) |
| SOFI | 2025-01-24 $18.18 | 2025-01-27 $15.84 | 2 | -12.87% | stop (gapped through at open) |
| AAL | 2025-01-28 $17.10 | 2025-02-13 $15.69 | 2 | -8.21% | stop |
| BAC | 2025-01-28 $45.31 | 2025-03-04 $41.49 | 1 | -8.41% | stop |
| SCHX | 2025-01-29 $23.47 | 2025-03-11 $21.64 | 2 | -7.82% | cut at -7.0% |
| PPL | 2025-02-12 $32.24 | 2025-04-07 $31.41 | 1 | -2.57% | stop |
| KGC | 2025-02-14 $11.26 | 2025-02-25 $10.45 | 4 | -7.23% | stop |
| NOK | 2025-02-26 $4.83 | 2025-04-04 $4.77 | 10 | -1.40% | stop (gapped through at open) |
| FXI | 2025-03-05 $34.86 | 2025-04-04 $32.18 | 1 | -7.71% | stop (gapped through at open) |
| T | 2025-03-11 $24.59 | 2025-04-07 $23.24 | 2 | -5.52% | stop (gapped through at open) |
| KGC | 2025-05-13 $13.67 | 2025-06-27 $14.81 | 3 | +8.31% | stop |
| GDX | 2025-05-13 $46.34 | 2025-06-24 $50.45 | 1 | +8.87% | stop |
| ERIC | 2025-05-13 $8.21 | 2025-06-23 $7.77 | 5 | -5.34% | stop (gapped through at open) |
| VZ | 2025-05-19 $40.01 | 2025-10-08 $37.89 | 1 | -5.30% | stop |
| SLV | 2025-06-24 $32.58 | 2025-10-17 $46.71 | 1 | +43.39% | stop |
| SOFI | 2025-06-25 $16.00 | 2025-07-14 $20.55 | 3 | +28.45% | stop |
| KGC | 2025-06-30 $14.86 | 2025-09-17 $22.26 | 3 | +49.73% | stop (gapped through at open) |
| CCL | 2025-07-15 $28.79 | 2025-09-29 $29.02 | 1 | +0.81% | stop |
| SOFI | 2025-09-18 $27.54 | 2025-09-25 $27.24 | 1 | -1.07% | stop |
| SOFI | 2025-09-26 $28.39 | 2025-09-30 $25.73 | 1 | -9.36% | stop |
| WBD | 2025-09-30 $18.99 | 2025-10-08 $17.73 | 2 | -6.65% | stop |
| CLF | 2025-10-01 $12.23 | 2025-10-10 $13.48 | 4 | +10.21% | stop |
| KGC | 2025-10-09 $25.15 | 2025-10-17 $24.91 | 2 | -0.92% | stop |
| WBD | 2025-10-09 $17.84 | 2025-11-13 $22.01 | 2 | +23.35% | stop |
| INTC | 2025-10-13 $36.50 | 2025-11-04 $38.10 | 1 | +4.40% | stop (gapped through at open) |
| KGC | 2025-10-20 $25.78 | 2025-10-21 $23.25 | 2 | -9.80% | stop |
| SLV | 2025-10-20 $47.86 | 2025-10-22 $43.70 | 1 | -8.70% | cut at -7.0% |
| LYFT | 2025-10-22 $20.62 | 2025-11-13 $23.73 | 2 | +15.10% | stop |
| KGC | 2025-10-27 $22.93 | 2025-12-02 $27.24 | 2 | +18.77% | stop |
| INTC | 2025-11-05 $37.46 | 2025-11-13 $35.53 | 1 | -5.15% | stop |
| WBD | 2025-11-14 $22.77 | 2025-12-17 $28.47 | 2 | +25.00% | stop |
| LYFT | 2025-11-14 $23.09 | 2025-11-18 $21.33 | 2 | -7.64% | stop (gapped through at open) |
| SLV | 2025-11-14 $45.80 | 2025-12-12 $55.32 | 1 | +20.80% | stop |
| VALE | 2025-11-19 $11.34 | 2026-01-30 $16.04 | 4 | +41.46% | stop (gapped through at open) |
| INTC | 2025-12-03 $43.55 | 2025-12-09 $40.72 | 1 | -6.51% | cut at -7.0% |
| AA | 2025-12-09 $43.35 | 2026-01-07 $61.11 | 1 | +40.95% | stop |
| HAL | 2025-12-15 $28.20 | 2026-01-28 $32.57 | 1 | +15.52% | stop |
| WBD | 2025-12-18 $28.38 | 2026-06-05 $26.52 | 1 | -6.54% | stop |
| SLB | 2026-01-08 $42.25 | 2026-01-30 $47.21 | 1 | +11.72% | stop |
| BMY | 2026-01-29 $53.06 | 2026-03-24 $54.75 | 1 | +3.17% | stop |
| VALE | 2026-02-02 $15.79 | 2026-02-17 $15.57 | 3 | -1.35% | stop |
| SLB | 2026-02-02 $46.95 | 2026-03-05 $46.61 | 1 | -0.72% | stop |
| VALE | 2026-02-18 $15.64 | 2026-03-03 $15.47 | 3 | -1.04% | stop |
| HAL | 2026-03-04 $34.67 | 2026-04-01 $37.71 | 1 | +8.77% | stop |
| PBR | 2026-03-06 $16.54 | 2026-04-01 $19.27 | 3 | +16.55% | stop |
| SLB | 2026-03-25 $50.18 | 2026-04-02 $48.73 | 1 | -2.89% | stop |
| PBR | 2026-04-02 $19.83 | 2026-04-17 $18.93 | 2 | -4.51% | stop |
| HAL | 2026-04-02 $38.18 | 2026-05-27 $38.84 | 1 | +1.74% | stop |
| NOK | 2026-04-06 $9.06 | 2026-04-22 $9.85 | 6 | +8.70% | stop |
| PBR | 2026-04-20 $19.85 | 2026-05-07 $19.29 | 2 | -2.84% | stop |
| NOK | 2026-04-23 $10.70 | 2026-05-07 $12.73 | 5 | +18.94% | stop (gapped through at open) |
| PBR | 2026-05-08 $19.90 | 2026-05-27 $18.13 | 2 | -8.91% | stop |
| HPE | 2026-05-08 $29.87 | 2026-05-28 $36.41 | 1 | +21.88% | stop |
| ERIC | 2026-05-28 $12.85 | 2026-06-09 $11.91 | 4 | -7.35% | stop (gapped through at open) |
| HAL | 2026-05-28 $39.28 | 2026-06-16 $37.10 | 1 | -5.56% | stop |
| F | 2026-06-03 $15.85 | 2026-06-09 $14.40 | 3 | -9.10% | stop |
| SLB | 2026-06-08 $55.52 | 2026-06-17 $51.29 | 1 | -7.60% | stop |
| BAC | 2026-06-10 $54.29 | 2026-09-14 $60.29 | 1 | +11.04% | stop |
| SCHB | 2026-06-10 $28.21 | open at end | 1 | - |  |
| KDP | 2026-06-18 $30.58 | 2026-07-09 $30.18 | 1 | -1.30% | stop |
| SCHF | 2026-06-18 $28.08 | open at end | 1 | - |  |
| AAL | 2026-07-10 $16.95 | 2026-07-15 $15.67 | 3 | -7.57% | cut at -7.0% |
| KHC | 2026-07-15 $24.60 | 2026-08-06 $24.85 | 2 | +1.00% | stop |
| HPQ | 2026-08-07 $27.93 | 2026-08-24 $28.65 | 2 | +2.60% | stop |
| HPQ | 2026-08-25 $28.45 | 2026-08-27 $27.35 | 1 | -3.85% | stop (gapped through at open) |
| HPQ | 2026-08-28 $29.64 | 2026-09-14 $33.66 | 1 | +13.55% | stop |
| HPQ | 2026-09-15 $33.94 | 2026-09-23 $31.59 | 1 | -6.92% | stop |
| PBR | 2026-09-15 $21.22 | 2026-10-26 $21.67 | 2 | +2.13% | stop (gapped through at open) |
| HPQ | 2026-09-24 $32.04 | 2026-10-12 $29.52 | 1 | -7.86% | stop |
| PFE | 2026-10-13 $28.52 | open at end | 1 | - |  |
| PBR | 2026-10-27 $22.62 | open at end | 2 | - |  |
