---
description: Read-only snapshot of the paper account, positions, open orders and stops
---

Read-only. No orders, no file writes.

1. python -m xahil account
2. python -m xahil positions
3. python -m xahil orders

Print one concise summary:

Portfolio (PAPER) - <today>
Equity: $X | Cash: $X (X%) | Exposure: X% of 20% cap
Positions: SYM | qty | entry -> now | unrealized P&L | stop
Open orders: TYPE | SYM | qty | stop/trail | id (first 8 chars)
Execution mode: dry-run or execute (from `python -m xahil preflight`)

Comment only if something is wrong: a position without a stop, a stop above the
price, or exposure or position size over its cap.
