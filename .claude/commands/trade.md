---
description: Check a manual paper trade against the risk engine. Usage - /trade SYMBOL DOLLARS buy|sell
---

Args: SYMBOL, DOLLARS (or SHARES for sells), SIDE. Ask if any are missing.

1. For a BUY: `python -m xahil check-buy SYMBOL --notional DOLLARS` and show every check.
   If it is rejected, stop and list the failed checks. Do not suggest ways around a limit.
2. Make sure today's memory/RESEARCH-LOG.md entry names SYMBOL with a catalyst. If not,
   ask me for the thesis and add it to today's entry first.
3. You cannot submit the order yourself. Show me the exact command to run in my own
   terminal, which asks me to type the symbol to confirm:
   `python -m xahil buy SYMBOL --notional DOLLARS --reason "<thesis>" --manual`
   (for sells: `python -m xahil sell SYMBOL --all --reason "<why>" --manual`)
4. After I run it, log the result to memory/TRADE-LOG.md and send
   `python -m xahil notify "<trade summary>"`.
