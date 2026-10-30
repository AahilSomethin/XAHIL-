---
description: Check a manual paper trade against the risk engine. Usage - /trade SYMBOL SHARES buy|sell
---

Args: SYMBOL, SHARES (whole shares), SIDE. Ask if any are missing.

1. For a BUY: run `python -m xahil size SYMBOL` (max whole shares within the 5% cap), then
   `python -m xahil check-buy SYMBOL --qty SHARES` and show every check. If it is rejected, stop and
   list the failed checks. Do not suggest ways around a limit. Fractional and dollar (notional) buys
   are refused by design: they cannot carry a GTC stop overnight.
2. Make sure today's memory/RESEARCH-LOG.md entry names SYMBOL with a catalyst. If not, ask me for
   the thesis, add it to today's entry, and record sources with `python -m xahil evidence`.
3. You cannot submit the order yourself. Show me the exact command to run in my own terminal, which
   asks me to type the symbol to confirm:
   `python -m xahil buy SYMBOL --qty SHARES --reason "<thesis>" --manual`
   then `python -m xahil protect SYMBOL --trail-pct 10 --manual` for the GTC stop.
   (sells: `python -m xahil sell SYMBOL --all --reason "<why>" --manual`)
4. After I run it, log the result to memory/TRADE-LOG.md, record
   `python -m xahil decide SYMBOL --action propose --reason "<thesis>"`, and send
   `python -m xahil notify "<trade summary>"`.
