"""Daily-bar backtester for the strategy rules, with an out-of-sample split.

Execution model (deliberately conservative, no lookahead):
  * Signals use data up to and including day t's close; entries fill at day
    t+1's OPEN plus slippage. The -7% cut is decided at a close and filled at
    the next open.
  * Stops are checked against each day's bar: if the day OPENS below the stop
    the fill is the open (an overnight gap blows through the stop); otherwise a
    low at or below the stop fills at the stop minus slippage.
  * The high-water mark and trailing stop update after the day's stop check,
    so a stop can only tighten for the next day.
  * Same caps as the live risk engine: 5% per position, 20% total exposure,
    3 new buys per ISO week, 4 positions, cash only, whole shares only.
  * Parameters are fixed in config, not fitted, so the out-of-sample window
    is an honest test of an unchanged rule set.
Not modelled: thesis-break exits, news, catalysts, partial fills, borrow
(no shorting exists), dividends beyond Alpaca's split/dividend adjustment.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from xahil import regime as regime_mod
from xahil.signals import evaluate_at
from xahil.ta import Indicators


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    shares: int
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    @property
    def pnl(self) -> float:
        return 0.0 if self.exit_price is None else (self.exit_price - self.entry_price) * self.shares

    @property
    def return_pct(self) -> float:
        return 0.0 if self.exit_price is None else (self.exit_price / self.entry_price - 1) * 100


@dataclass
class Result:
    start: str
    end: str
    equity_curve: list = field(default_factory=list)  # (date, equity, exposure)
    trades: list = field(default_factory=list)
    rejected_entries: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)


def _bar(ind: Indicators, date: str) -> Optional[dict]:
    i = ind.at(date)
    return None if i is None else ind.bars[i]


def _trail_for_gain(gain_pct: float, strat: dict, current: float) -> float:
    trail = strat_initial_trail(strat)
    for tier in sorted(strat["trail_tightening"], key=lambda t: -t["gain_pct"]):
        if gain_pct >= tier["gain_pct"]:
            trail = tier["trail_pct"]
            break
    return min(current, trail)  # never loosen


def strat_initial_trail(strat: dict) -> float:
    return float(strat["initial_stop_pct"])


def run_backtest(bars_by_symbol: dict, bench_bars: list, sig: dict, strat: dict, limits, start: str, end: str,
                 starting_cash: float = 100000.0, regime_filter: bool = False) -> Result:
    bt = sig["backtest"]
    slip = bt["slippage_bps"] / 10000
    commission = float(bt.get("commission_per_order", 0))
    bench = Indicators(bench_bars, sig)
    inds = {s: Indicators(b, sig) for s, b in bars_by_symbol.items() if b}
    calendar = [d for d in bench.dates if start <= d <= end]
    res = Result(start=calendar[0] if calendar else start, end=calendar[-1] if calendar else end)
    cash = starting_cash
    positions: dict = {}
    pending_exits: dict = {}
    pending_entries: list = []
    week_buys: dict = {}
    min_gap = float(strat.get("min_stop_distance_pct", 3.0)) / 100
    cut = float(strat["cut_loss_pct"])

    def bump(reason):
        res.rejected_entries[reason] = res.rejected_entries.get(reason, 0) + 1

    def close_position(sym, date, price, reason):
        nonlocal cash
        p = positions.pop(sym)
        fill = price * (1 - slip)
        cash += p["shares"] * fill - commission
        tr = p["trade"]
        tr.exit_date, tr.exit_price, tr.exit_reason = date, round(fill, 4), reason

    for d in calendar:
        # 1) exits decided at the previous close, filled at today's open
        for sym, reason in list(pending_exits.items()):
            bar = _bar(inds[sym], d)
            if bar is None or sym not in positions:
                continue
            close_position(sym, d, float(bar["o"]), reason)
            pending_exits.pop(sym)

        # 2) entries from the previous close's signals, filled at today's open
        equity_open = cash + sum(p["shares"] * float((_bar(inds[s], d) or {"o": p["last"]})["o"])
                                 for s, p in positions.items())
        iso_week = dt.date.fromisoformat(d).isocalendar()[:2]
        for sym, signal in pending_entries:
            bar = _bar(inds[sym], d)
            if bar is None:
                bump("no bar on entry day")
                continue
            if sym in positions:
                continue
            if week_buys.get(iso_week, 0) >= limits.max_new_trades_per_week:
                bump("weekly buy cap")
                continue
            if len(positions) >= limits.max_open_positions:
                bump("max positions")
                continue
            price = float(bar["o"]) * (1 + slip)
            exposure = sum(p["shares"] * float((_bar(inds[s], d) or {"o": p["last"]})["o"]) for s, p in positions.items())
            budget = min(equity_open * limits.max_position_pct / 100,
                         equity_open * limits.max_total_exposure_pct / 100 - exposure,
                         cash - commission)
            shares = math.floor(budget / price) if budget > 0 else 0
            if shares < 1:
                bump("no whole share fits caps/cash")
                continue
            cash -= shares * price + commission
            week_buys[iso_week] = week_buys.get(iso_week, 0) + 1
            stop = price * (1 - strat_initial_trail(strat) / 100)
            trade = Trade(sym, d, round(price, 4), shares)
            res.trades.append(trade)  # recorded at entry; exit fields are filled when it closes
            positions[sym] = {"shares": shares, "entry_price": round(price, 4), "entry_date": d, "stop": stop,
                              "hwm": price, "trail": strat_initial_trail(strat), "last": price, "trade": trade}
        pending_entries = []

        # 3) intraday stops (gap at the open fills at the open)
        for sym in list(positions):
            bar = _bar(inds[sym], d)
            if bar is None:
                continue
            p = positions[sym]
            o, low = float(bar["o"]), float(bar["l"])
            if o <= p["stop"]:
                close_position(sym, d, o, "stop (gapped through at open)")
            elif low <= p["stop"]:
                close_position(sym, d, p["stop"], "stop")

        # 4) at the close: update trailing stops, decide -7% cuts
        for sym, p in positions.items():
            bar = _bar(inds[sym], d)
            if bar is None:
                continue
            c, h = float(bar["c"]), float(bar["h"])
            p["last"] = c
            p["hwm"] = max(p["hwm"], h)
            gain = (c / p["entry_price"] - 1) * 100
            p["trail"] = _trail_for_gain(gain, strat, p["trail"])
            candidate = p["hwm"] * (1 - p["trail"] / 100)
            candidate = min(candidate, c * (1 - min_gap))   # never within 3% of price
            p["stop"] = max(p["stop"], candidate)            # never move down
            if gain <= cut:
                pending_exits[sym] = f"cut at {cut}%"

        # 5) signals at the close for tomorrow's open
        bi = bench.at(d)
        reg = regime_mod.classify_at(bench, bi, sig).regime if bi is not None else regime_mod.UNKNOWN
        equity_close = cash + sum(p["shares"] * p["last"] for p in positions.values())
        if not (regime_filter and reg in (regime_mod.RISK_OFF, regime_mod.HIGH_VOL)):
            cands = []
            for sym, ind in inds.items():
                if sym in positions or sym in pending_exits:
                    continue
                i = ind.at(d)
                if i is None:
                    continue
                s = evaluate_at(sym, ind, i, bench, sig, position_budget=equity_close * limits.max_position_pct / 100,
                                price_buffer_pct=limits.price_buffer_pct, regime=reg)
                if s.qualified:
                    rs = next((c.value for c in s.checks if c.name == "relative_strength"), 0) or 0
                    cands.append((rs, sym, s))
            pending_entries = [(sym, s) for _, sym, s in sorted(cands, reverse=True)]
        exposure_close = sum(p["shares"] * p["last"] for p in positions.values())
        res.equity_curve.append((d, round(equity_close, 2), round(exposure_close, 2)))

    # mark open positions at the last close (not counted as closed trades)
    res.metrics = metrics(res, bench, starting_cash)
    res.metrics["open_positions_at_end"] = {s: p["shares"] for s, p in positions.items()}
    return res


def metrics(res: Result, bench: Indicators, starting_cash: float) -> dict:
    if not res.equity_curve:
        return {"available": False}
    eq = [e for _, e, _ in res.equity_curve]
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = min(mdd, (e / peak - 1) * 100)
    days = (dt.date.fromisoformat(res.equity_curve[-1][0]) - dt.date.fromisoformat(res.equity_curve[0][0])).days
    total = (eq[-1] / starting_cash - 1) * 100
    years = days / 365.25 if days > 0 else 0
    cagr = ((eq[-1] / starting_cash) ** (1 / years) - 1) * 100 if years > 0 and eq[-1] > 0 else None
    closed = [t for t in res.trades if t.exit_price is not None]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl < 0]
    gw, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
    b0, b1 = bench.at(res.equity_curve[0][0]), bench.at(res.equity_curve[-1][0])
    bench_ret = (bench.close[b1] / bench.close[b0] - 1) * 100 if b0 is not None and b1 is not None else None
    holds = [(dt.date.fromisoformat(t.exit_date) - dt.date.fromisoformat(t.entry_date)).days for t in closed]
    gaps = [t for t in closed if t.exit_reason and "gapped" in t.exit_reason]
    return {
        "available": True,
        "start": res.equity_curve[0][0], "end": res.equity_curve[-1][0], "trading_days": len(eq),
        "start_equity": starting_cash, "end_equity": eq[-1],
        "total_return_pct": round(total, 2), "cagr_pct": None if cagr is None else round(cagr, 2),
        "max_drawdown_pct": round(mdd, 2),
        "benchmark_return_pct": None if bench_ret is None else round(bench_ret, 2),
        "excess_vs_benchmark_pct": None if bench_ret is None else round(total - bench_ret, 2),
        # Fairer yardstick for a strategy capped at 20% exposure: SPY scaled by the strategy's average exposure.
        "exposure_matched_benchmark_pct": None if bench_ret is None else round(
            bench_ret * (sum(x / e for _, e, x in res.equity_curve if e) / len(eq)), 2),
        "entries": len(res.trades), "closed_trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "avg_trade_return_pct": round(sum(t.return_pct for t in closed) / len(closed), 2) if closed else None,
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
        "avg_exposure_pct": round(sum(x / e * 100 for _, e, x in res.equity_curve if e) / len(eq), 2),
        "gap_through_stop_exits": len(gaps),
        "worst_trade_pct": round(min(t.return_pct for t in closed), 2) if closed else None,
        "rejected_entries": dict(res.rejected_entries),
    }


MIN_TRADES_FOR_EVIDENCE = 20


def out_of_sample(bars_by_symbol: dict, bench_bars: list, sig: dict, strat: dict, limits, start: str, end: str,
                  starting_cash: float = 100000.0, regime_filter: bool = False) -> dict:
    """Run the unchanged rules on an in-sample window and a later out-of-sample window."""
    dates = [str(b["t"])[:10] for b in bench_bars if start <= str(b["t"])[:10] <= end]
    if len(dates) < 60:
        return {"error": "not enough benchmark history in the requested window"}
    split_i = int(len(dates) * (1 - sig["backtest"]["out_of_sample_fraction"]))
    split = dates[split_i]
    ins = run_backtest(bars_by_symbol, bench_bars, sig, strat, limits, dates[0], dates[split_i - 1],
                       starting_cash, regime_filter)
    oos = run_backtest(bars_by_symbol, bench_bars, sig, strat, limits, split, dates[-1], starting_cash, regime_filter)
    return {"split_date": split, "in_sample": ins, "out_of_sample": oos,
            "assessment": assess(ins.metrics, oos.metrics)}


def assess(ins: dict, oos: dict) -> dict:
    flags = []
    if not oos.get("available"):
        return {"verdict": "no data", "flags": ["out-of-sample window empty"]}
    if (oos.get("closed_trades") or 0) < MIN_TRADES_FOR_EVIDENCE:
        flags.append(f"only {oos.get('closed_trades', 0)} out-of-sample trades (< {MIN_TRADES_FOR_EVIDENCE}): "
                     f"not statistically meaningful")
    if (oos.get("total_return_pct") or 0) < 0:
        flags.append("out-of-sample return is negative")
    if oos.get("excess_vs_benchmark_pct") is not None and oos["excess_vs_benchmark_pct"] < 0:
        flags.append("out-of-sample return trails the S&P 500 (SPY)")
    em = oos.get("exposure_matched_benchmark_pct")
    if em is not None and (oos.get("total_return_pct") or 0) < em:
        flags.append(f"out-of-sample return also trails exposure-matched SPY ({em}%)")
    if oos.get("profit_factor") is not None and oos["profit_factor"] < 1:
        flags.append("out-of-sample profit factor below 1")
    if ins.get("total_return_pct") is not None and oos.get("total_return_pct") is not None \
            and ins["total_return_pct"] > 0 and oos["total_return_pct"] < ins["total_return_pct"] / 2:
        flags.append("out-of-sample return is less than half of in-sample (possible decay or overfit)")
    if (oos.get("gap_through_stop_exits") or 0) > 0:
        flags.append(f"{oos['gap_through_stop_exits']} stop exits gapped through the stop at the open")
    ins_bad = (ins.get("total_return_pct") is not None and ins["total_return_pct"] < 0) or \
        (ins.get("profit_factor") is not None and ins["profit_factor"] < 1)
    if ins_bad:
        flags.append(f"in-sample period fails (return {ins.get('total_return_pct')}%, profit factor "
                     f"{ins.get('profit_factor')}): results are not consistent across periods")
    if not flags:
        verdict = "passes basic out-of-sample checks (still not proof of an edge)"
    elif any(("negative" in f or "below 1" in f) and f.startswith("out-of-sample") for f in flags):
        verdict = "fails out-of-sample"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "flags": flags}


def report_markdown(result: dict, universe: list, regime_filter: bool, generated: str) -> str:
    def table(m: dict) -> list:
        keys = ["start", "end", "trading_days", "end_equity", "total_return_pct", "cagr_pct", "max_drawdown_pct",
                "benchmark_return_pct", "excess_vs_benchmark_pct", "exposure_matched_benchmark_pct", "closed_trades", "win_rate_pct", "profit_factor",
                "avg_trade_return_pct", "avg_hold_days", "avg_exposure_pct", "gap_through_stop_exits",
                "worst_trade_pct"]
        rows = ["| Metric | Value |", "|---|---|"]
        rows += [f"| {k} | {m.get(k, 'n/a')} |" for k in keys]
        rows.append(f"| rejected_entries | {m.get('rejected_entries', {})} |")
        return rows

    a = result["assessment"]
    lines = [f"# Backtest {generated} (PAPER research, advisory signal model)", "",
             f"- Universe ({len(universe)}): {', '.join(universe)}",
             f"- Regime filter: {'on (no entries in risk_off / high_volatility)' if regime_filter else 'off'}",
             f"- Split date (out-of-sample starts): {result['split_date']}",
             "- Fixed parameters from config/signal_params.json and config/strategy_params.json (no fitting).",
             "- Execution: next-day open + slippage; gaps through stops fill at the open; whole shares; caps as live.",
             "- Caveats: survivorship bias (today's universe), IEX-feed bars, no catalysts/thesis exits modelled.", "",
             f"## Assessment: {a['verdict']}"] + [f"- {f}" for f in a["flags"]] + \
            ["", "## In-sample"] + table(result["in_sample"].metrics) + \
            ["", "## Out-of-sample"] + table(result["out_of_sample"].metrics) + \
            ["", "## Out-of-sample trades", "| Symbol | Entry | Exit | Shares | Return | Reason |", "|---|---|---|---|---|---|"]
    for t in result["out_of_sample"].trades[:200]:
        exit_txt = f"{t.exit_date} ${t.exit_price:.2f}" if t.exit_price is not None else "open at end"
        ret = f"{t.return_pct:+.2f}%" if t.exit_price is not None else "-"
        lines.append(f"| {t.symbol} | {t.entry_date} ${t.entry_price:.2f} | {exit_txt} | {t.shares} | {ret} | "
                     f"{t.exit_reason or ''} |")
    return "\n".join(lines) + "\n"


def summary_dict(result: dict) -> dict:
    return {"split_date": result["split_date"], "assessment": result["assessment"],
            "in_sample": result["in_sample"].metrics, "out_of_sample": result["out_of_sample"].metrics}


def trades_as_dicts(trades: list) -> list:
    return [{**asdict(t), "return_pct": round(t.return_pct, 2)} for t in trades]
