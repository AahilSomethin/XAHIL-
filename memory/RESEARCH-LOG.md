# Research Log (PAPER)

One dated entry per trading day. The risk engine refuses a buy unless the
symbol appears in **today's** entry, so the heading format matters:
`## YYYY-MM-DD - Pre-market Research` (date first).

Template:

## YYYY-MM-DD - Pre-market Research

### Account
- Equity / cash / long exposure / buys this week (from `xahil reconcile`)

### Market Context
- S&P 500 futures / SPY:
- VIX:
- Rates, oil, dollar (if relevant):
- Today's catalysts and economic calendar:
- Earnings before the open:
- Sector momentum:
- Research source: Perplexity (cited) | WebSearch fallback

### Held Positions
- SYM: news, thesis still intact?

### Trade Ideas
1. SYM - catalyst, entry ~$X, stop $X, target $X, R:R X:1, size $N (<= 5% equity), sources

### Risk Factors
- ...

### Decision
TRADE or HOLD (default HOLD)

## 2026-11-02 - Pre-market Research

### Account
- Equity $100,000 / cash $100,000 / long exposure $0 / no positions / 0 buys this week. Reconcile OK, no halts. Dry-run mode.

### Market Context
- Regime: neutral (SPY above SMA200, mixed trend; 20d vol 11.7%).
- S&P 500 futures / SPY: futures up early Monday after Fri -1.8% close (S&P 7,580) [e-2026-11-02-1, -2].
- VIX: ~19.3-19.4 (Fri close 19.42; spiked early) [e-2026-11-02-2, -6].
- Rates, oil, dollar: WTI ~$99.7 (-4.2%), Brent ~$108.9; DXY 101.5 [e-2026-11-02-1]. Oil elevated on Iran war/shipping blockade [e-2026-11-02-16].
- Today's catalysts and economic calendar: ISM manufacturing today; midterms Tue; Apple/Palantir earnings; Fri NFP (Sept was weak) [e-2026-11-02-8, -9, -10].
- Earnings before the open: MAR; AMD Tue AMC, UBER/MCD Wed BMO [e-2026-11-02-7, -8].
- Sector momentum: October AI-stock slump weighing on market [e-2026-11-02-11]; energy supported by oil.
- Research source: Tavily

### Held Positions
- None.

### Signals considered
- Qualified: AES, DVN, KMI, KO, OXY, PFE, SLB, XLP. Researched OXY, DVN, KO (budget 5/8 used).
- OXY: insufficient (1 source) -> skip. KO: insufficient (1 undated source) -> skip.
- DVN: sufficient, but earnings Thu Nov 5 (binary gap risk, stops can gap), oil fell -4% today, pre-market spread 4.5%. No dated catalyst worth entering ahead of print -> watch.

### Trade Ideas
- None. DVN check-buy (95 sh, ~$4.9k) REJECTED: market closed, stale quote, spread, no catalyst in log (expected pre-open; catalyst deliberately not claimed).

### Risk Factors
- Midterms, NFP Fri, AI-stock weakness, oil volatility, Friday -1.8% sell-off.

### Decision
HOLD

## 2026-11-03 - Pre-market Research

### Account
- Equity $100,000 / cash $100,000 / long exposure $0 / no positions / 0 buys this week. Reconcile OK, no halts. Dry-run mode.

### Market Context
- Regime: neutral (SPY above SMA200, SMA50 > SMA200, mixed trend; 20d vol 11.4%).
- S&P 500 futures / SPY: Mon close S&P ~7,629 (+0.65%) as WTI fell ~6% [e-2026-11-03-1]; futures mixed/slightly lower Tue ahead of midterm results; chipmaker sell-off, Apple/Palantir (-8.9% pre-mkt) slide [e-2026-11-03-4, -9].
- VIX: ~19-21 (prior close 21.15; VIX1D 19.3, VVIX 106) [e-2026-11-03-2, -3].
- Rates, oil, dollar: oil volatile on Iran war diplomacy headlines [e-2026-11-03-5].
- Today's catalysts and economic calendar: US midterms; JOLTS + factory orders 10:00 ET; NFP Fri [e-2026-11-03-6, -7].
- Earnings: PFE, SHOP before open; AMD, ANET, AMGN, BKNG after close [e-2026-11-03-6, -7].
- Sector momentum: AI/tech wobble after October slump; defensives/energy relatively steadier [e-2026-11-03-8].
- Research source: Tavily

### Held Positions
- None.

### Signals considered
- Qualified: GM, KMI, KO, MO, OXY, PFE, WFC, XLF, XLP. Researched PFE, KMI, WFC (5/8 credits used).
- KMI, WFC: insufficient (single-domain, undated junk) -> skip.
- PFE: sufficient, but reports Q3 earnings before today's open (binary gap risk; stops can gap) [e-2026-11-03-11..18] -> watch, revisit after the print.

### Trade Ideas
- None. No dated catalyst justifies entry into a binary earnings event, plus midterms day.

### Risk Factors
- Midterms, JOLTS, AMD earnings AMC, NFP Fri, AI-stock weakness, oil/Iran headline risk.

### Decision
HOLD
