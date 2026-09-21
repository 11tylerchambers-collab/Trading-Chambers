"""Nightly parameter sweep (spec §10).

Replays every grid combination over the last N session days of stored bars,
scores by total net PnL, requires ≥ 20 trades/day on average for eligibility,
then moves each parameter at most one grid step toward the best combination.
No change at all if today produced fewer than 20 closed trades.
"""
from __future__ import annotations

import itertools
import logging
import time
from datetime import date, datetime
from typing import Optional

from .clock import Session
from .replay import DayData, build_day, replay
from .store import Store
from .strategy import Params

log = logging.getLogger("chambers.sweep")

GRID: dict[str, list] = {
    "entry_dev_pct": [0.20, 0.30, 0.40, 0.50, 0.60, 0.80],
    "vol_mult": [1.00, 1.25, 1.50, 2.00],
    "max_hold_bars": [5, 10, 15, 20],
    "stop_pct": [0.30, 0.50, 0.75],
}
N_DAYS = 5
MIN_TRADES_PER_DAY = 20      # eligibility of a combination
MIN_TODAY_TRADES = 20        # evidence needed to change anything


def nearest_index(values: list, x) -> int:
    return min(range(len(values)), key=lambda i: (abs(values[i] - x), i))


def one_step(current: Params, best: Params) -> Params:
    """Each grid parameter moves at most one grid step from `current` toward `best`."""
    out = {}
    for key, values in GRID.items():
        ci = nearest_index(values, getattr(current, key))
        bi = nearest_index(values, getattr(best, key))
        if bi > ci:
            ni = ci + 1
        elif bi < ci:
            ni = ci - 1
        else:
            ni = ci
        out[key] = values[ni]
    return current.replace(**out)


def grid_combos(base: Params) -> list[Params]:
    keys = list(GRID)
    return [base.replace(**dict(zip(keys, vals))) for vals in itertools.product(*(GRID[k] for k in keys))]


def score_combo(days: list[DayData], params: Params) -> dict:
    net = 0.0
    trades = 0
    for day in days:
        r = replay(day, params, collect_hypothesis=False)
        net += r.net_pnl
        trades += len(r.trades)
    per_day = trades / len(days) if days else 0.0
    return {"params": {k: getattr(params, k) for k in GRID}, "net_pnl": round(net, 2), "trades": trades,
            "trades_per_day": round(per_day, 2), "eligible": per_day >= MIN_TRADES_PER_DAY}


def evaluate_grid(days: list[DayData], base: Params) -> list[dict]:
    return [score_combo(days, p) for p in grid_combos(base)]


def load_days(store: Store, dates: list[str], sessions: Optional[dict[date, Session]] = None) -> list[DayData]:
    days = []
    for ds in dates:
        d = date.fromisoformat(ds)
        sess = (sessions or {}).get(d)
        days.append(build_day(d, store.bars_for_day(d), sess))
    return days


def run_sweep(store: Store, current: Params, today: date, now: datetime,
              sessions: Optional[dict[date, Session]] = None,
              min_today_trades: int = MIN_TODAY_TRADES, n_days: int = N_DAYS) -> dict:
    """Run the sweep, write `params` (only if changed) and `params_history`. Returns the summary."""
    t0 = time.monotonic()
    dates = store.bar_dates(n_days)
    today_closed = len(store.closed_trades_for_day(today))
    summary: dict = {"date": today.isoformat(), "days": dates, "current": current.to_dict(),
                     "today_closed_trades": today_closed, "combos_evaluated": 0, "eligible": 0,
                     "best": None, "best_score": None, "chosen": current.to_dict(), "changed": False,
                     "reason": "", "results": []}
    if not dates:
        summary["reason"] = "no_data: no bars stored yet"
    else:
        days = load_days(store, dates, sessions)
        results = evaluate_grid(days, current)
        results.sort(key=lambda r: (-r["eligible"], -r["net_pnl"]))
        eligible = [r for r in results if r["eligible"]]
        summary["results"] = results
        summary["combos_evaluated"] = len(results)
        summary["eligible"] = len(eligible)
        best = eligible[0] if eligible else None
        if best is not None:
            summary["best"] = best["params"]
            summary["best_score"] = best["net_pnl"]
        if today_closed < min_today_trades:
            summary["reason"] = (f"insufficient_evidence: {today_closed} closed trades today "
                                 f"(< {min_today_trades}); params unchanged")
        elif best is None:
            summary["reason"] = f"no_eligible_combination: none averaged >= {MIN_TRADES_PER_DAY} trades/day"
        else:
            best_params = current.replace(**best["params"])
            chosen = one_step(current, best_params)
            summary["chosen"] = chosen.to_dict()
            if chosen == current:
                summary["reason"] = "best_is_current: params unchanged"
            else:
                moved = {k: (getattr(current, k), getattr(chosen, k)) for k in GRID
                         if getattr(current, k) != getattr(chosen, k)}
                capped = chosen != best_params
                summary["changed"] = True
                summary["reason"] = ("moved_one_step_toward_best" if capped else "moved_to_best") + \
                                    ": " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in moved.items())
    summary["duration_s"] = round(time.monotonic() - t0, 1)
    chosen = Params.from_dict(summary["chosen"])
    if summary["changed"]:
        store.write_params(chosen.to_dict(), "sweep", now)
    store.write_params_history(today, chosen.to_dict(), "sweep", summary)
    log.info("sweep %s: %s (%.1fs, %d combos, %d eligible)", today, summary["reason"], summary["duration_s"],
             summary["combos_evaluated"], summary["eligible"])
    return summary
