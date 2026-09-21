"""Phase 0 acceptance gate (spec §13, items 1–6) over the last 5 sessions in the database."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .clock import ET, Session
from .replay import default_session
from .store import Store, parse_ts

MIN_CYCLE_COVERAGE = 0.98
MIN_CLOSED_TRADES = 30
MIN_SIGNAL_COVERAGE = 0.98
REQUIRED_TRADE_FIELDS = ("hypothesis_json", "exit_reason", "mae_pct", "mfe_pct", "est_cost", "net_pnl")


def run_gate(store: Store, sessions: Optional[dict[date, Session]] = None, universe_size: int = 20,
             n_sessions: int = 5) -> dict:
    dates = store.cycle_dates(n_sessions)
    items = []
    if not dates:
        for i, name in enumerate(("cycle coverage", "closed trades", "trade fields", "signal coverage",
                                  "sweep ran", "no unhandled errors"), 1):
            items.append({"id": i, "name": name, "pass": False, "detail": "no sessions in database"})
        return {"sessions": [], "items": items}

    per_day = []
    for ds in dates:
        d = date.fromisoformat(ds)
        sess = (sessions or {}).get(d) or default_session(d)
        minutes = sess.cycle_minutes()
        n_min = len(minutes)
        cycles = store.cycles_for_day(d)
        cyc_minutes = {parse_ts(c["ts"]).replace(second=0, microsecond=0) for c in cycles}
        covered = sum(1 for m in minutes if m in cyc_minutes)
        closed = store.closed_trades_for_day(d)
        missing = [t.id for t in closed if any(getattr(t, f) is None for f in REQUIRED_TRADE_FIELDS)]
        sigs = [s for s in store.signals_for_day(d) if sess.open <= parse_ts(s["ts"]) < sess.flatten_at]
        swept = ds in store.params_history_dates("sweep")
        unhandled = store.errors_count(where="unhandled", d=d)
        per_day.append({"date": ds, "minutes": n_min, "cycle_minutes": covered,
                        "cycle_coverage": covered / n_min if n_min else 0.0,
                        "closed": len(closed), "missing_fields": missing,
                        "signals": len(sigs), "signal_coverage": len(sigs) / (n_min * universe_size) if n_min else 0.0,
                        "swept": swept, "unhandled": unhandled})

    def item(i, name, ok_fn, fmt):
        oks = [ok_fn(p) for p in per_day]
        detail = "; ".join(f"{p['date']}: {fmt(p)}{'' if ok else ' FAIL'}" for p, ok in zip(per_day, oks))
        items.append({"id": i, "name": name, "pass": all(oks), "detail": detail})

    item(1, f"cycles cover >= {MIN_CYCLE_COVERAGE:.0%} of minutes open..flatten",
         lambda p: p["cycle_coverage"] >= MIN_CYCLE_COVERAGE,
         lambda p: f"{p['cycle_minutes']}/{p['minutes']} ({p['cycle_coverage']:.1%})")
    item(2, f">= {MIN_CLOSED_TRADES} closed trades per day",
         lambda p: p["closed"] >= MIN_CLOSED_TRADES, lambda p: f"{p['closed']} closed")
    item(3, "every closed trade has hypothesis, exit_reason, mae, mfe, est_cost, net_pnl",
         lambda p: not p["missing_fields"],
         lambda p: "all complete" if not p["missing_fields"] else f"incomplete trade ids {p['missing_fields'][:10]}")
    item(4, f"signals >= {MIN_SIGNAL_COVERAGE:.0%} of minutes x {universe_size} symbols",
         lambda p: p["signal_coverage"] >= MIN_SIGNAL_COVERAGE,
         lambda p: f"{p['signals']} ({p['signal_coverage']:.1%})")
    item(5, "sweep wrote params_history each night", lambda p: p["swept"],
         lambda p: "swept" if p["swept"] else "no sweep row")
    item(6, "zero errors with where_ = 'unhandled'", lambda p: p["unhandled"] == 0,
         lambda p: f"{p['unhandled']} unhandled")
    return {"sessions": dates, "items": items, "per_day": per_day}


def format_gate(results: dict) -> str:
    lines = [f"Phase 0 gate over sessions: {', '.join(results['sessions']) or '(none)'}", ""]
    for it in results["items"]:
        lines.append(f"[{'PASS' if it['pass'] else 'FAIL'}] {it['id']}. {it['name']}")
        lines.append(f"       {it['detail']}")
    overall = all(it["pass"] for it in results["items"]) and len(results["sessions"]) >= 5
    lines.append("")
    if len(results["sessions"]) < 5:
        lines.append(f"NOTE: only {len(results['sessions'])} session(s) in the database; the gate needs 5 consecutive.")
    lines.append("OVERALL: " + ("PASS" if overall else "FAIL"))
    lines.append("Items 7 (heartbeat spot checks) and 8 (mid-session restart) are verified by hand.")
    return "\n".join(lines)
