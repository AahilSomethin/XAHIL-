# Trading Strategy (PAPER)

> Human-owned. Scheduled runs must never edit this file. Proposed changes go to
> memory/STRATEGY-PROPOSALS.md and take effect only after the owner edits this
> file (and config/strategy_params.json) by hand.

## Mission
Learn whether a disciplined, research-driven swing strategy can beat the S&P 500
(SPY) on a **$100,000 Alpaca paper account** (baseline set 2026-11-01). No real money. Patience beats activity.

## Hard Risk Limits (enforced in code by `xahil` - not negotiable)
1. Paper endpoint only (`https://paper-api.alpaca.markets/v2`).
2. Cash only. No margin, no leverage, no short selling.
3. Long-only US-listed stocks and ETFs. No options, futures, crypto, OTC,
   leveraged, inverse or volatility products.
4. Max **5%** of equity per position (about $5,000 at start).
5. Max **20%** total long exposure (about $20,000 at start). The other 80% stays in cash.
6. Max **3 new buy orders per week** (Mon-Sun ET), counted from Alpaca.
7. Max 4 open positions.

## Strategy Rules (followed by Claude; parameters in config/strategy_params.json)
1. Every buy needs a specific, dated catalyst written in today's RESEARCH-LOG entry first.
2. Follow sector momentum. Don't force a thesis if the sector is rolling over.
3. Initial protective stop 10% below entry.
   - Whole-share positions: 10% trailing stop, GTC.
   - Fractional positions: Alpaca allows DAY stops only, so a fresh stop is placed each market-open.
4. Cut any position at -7% from entry. No averaging down.
5. Tighten the trail to 7% at +15%, and to 5% at +20%.
6. Never place a stop within 3% of the current price. Never move a stop down.
7. Exit a sector after 2 consecutive failed trades in it.
8. Exit early if the thesis breaks (catalyst invalidated, sector rolls over, news).
9. Minimum 2:1 reward-to-risk at entry.
10. Default decision is HOLD. A week with zero trades can be the right answer.

## Entry Checklist (document before any buy)
- Catalyst today?
- Sector in momentum?
- Stop level (7-10% below entry)?
- Target (at least 2:1 R:R)?
- `python3 -m xahil check-buy SYM --notional N` approved?
