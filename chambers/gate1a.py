"""Phase 1A gate (PHASE1A §15), checked from the database over the last 10 trading days.

Items 1–6 and the `unhandled` half of 7 are automatic; the mid-session restart with an overnight S3
position (rest of 7) is verified by hand and printed as a reminder.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .clock import ET, Session
from .replay import default_session
from .store import Store, parse_ts
from .watchdog import expected_marks

N_DAYS = 10
MIN_COVERAGE = 0.98
MESSAGE_RATE = 0.95
TRADE_FIELDS = ("hypothesis_json", "exit_reason", "est_cost", "net_pnl", "news_day", "mae_pct", "mfe_pct")
TWIN_FIELDS = ("hypothesis_json", "exit_reason", "est_cost", "net_pnl", "news_day")


def run_gate_1a(store: Store, sessions: Optional[dict] = None, sleeves: tuple = ("S0", "S1", "S2", "S3"),
                n_days: int = N_DAYS) -> dict:
    dates = store.cycle_dates(n_days, "S0")
    items = []

    def add(i, name, ok, detail):
        items.append({"id": i, "name": name, "pass": bool(ok) and bool(dates), "detail": detail or "no sessions"})

    sess = {ds: (sessions or {}).get(date.fromisoformat(ds)) or default_session(date.fromisoformat(ds)) for ds in dates}
    # 1. cycle coverage per sleeve
    cov, bad = [], []
    for ds in dates:
        s = sess[ds]
        for sid in sleeves:
            if sid == "S2":
                start = datetime.fromisoformat(ds + "T00:00:00").replace(tzinfo=ET)
                marks = expected_marks("S2", None, start, start + timedelta(days=1))
            else:
                marks = expected_marks(sid, s, s.open, s.close + timedelta(minutes=1))
            if not marks:
                continue
            got = {parse_ts(c["ts"]) for c in store.cycles_between(sid, marks[0] - timedelta(seconds=5),
                                                                   marks[-1] + timedelta(minutes=10))}
            hit = sum(1 for m in marks if any(m - timedelta(seconds=5) <= g < m + timedelta(seconds=55) for g in got))
            r = hit / len(marks)
            cov.append(f"{ds} {sid} {hit}/{len(marks)}")
            if r < MIN_COVERAGE:
                bad.append(f"{ds} {sid} {r:.1%}")
    add(1, f"every active sleeve wrote >= {MIN_COVERAGE:.0%} of its expected cycles", not bad,
        ("FAIL " + ", ".join(bad[:8])) if bad else f"{len(cov)} sleeve-days all >= 98%")
    # 2. trade counts
    s0 = {ds: len(store.closed_trades_for_day(ds, "S0")) for ds in dates}
    s1 = sum(len(store.closed_trades_for_day(ds, "S1")) for ds in dates) / len(dates) if dates else 0
    ok2 = all(v >= 30 for v in s0.values()) and s1 >= 1
    add(2, "S0 >= 30 closed trades/day; S1 >= 1/day average", ok2,
        f"S0 per day {list(s0.values())}; S1 average {s1:.2f}/day")
    # 3. complete fields
    missing = []
    for ds in dates:
        for t in store.closed_trades_for_day(ds, None):
            if any(getattr(t, f) is None for f in TRADE_FIELDS):
                missing.append(f"trade {t.id}")
        for sid in sleeves:
            for t in store.twin_trades_for_day(ds, sid):
                if any(t.get(f) is None for f in TWIN_FIELDS):
                    missing.append(f"twin {t['id']}")
    add(3, "every closed trade and twin trade has hypothesis, exit_reason, costs, news_day", not missing,
        ("incomplete: " + ", ".join(missing[:10])) if missing else "all complete")
    # 4. recon
    rec = {ds: [r["status"] for r in store.recon_for_day(ds) if r["kind"] == "equity_session"] for ds in dates}
    bad4 = [f"{ds}: {v or 'none'}" for ds, v in rec.items() if not v or any(x not in ("pass", "baseline") for x in v)]
    add(4, "recon passed every day (mismatches must be explained and fixed by hand)", not bad4,
        ("FAIL " + "; ".join(bad4)) if bad4 else "pass every day")
    # 5. nightly work
    bad5 = []
    for ds in dates:
        for sid in ("S0", "S1", "S3"):
            if sid in sleeves and not store.has_sweep_for(ds, sid):
                bad5.append(f"{ds} {sid} sweep")
        if "S2" in sleeves and not store.has_sweep_for(ds, "S2"):
            bad5.append(f"{ds} S2 sweep")
        if not any((r["started_at"] or "")[:10] == ds for r in store.lab_runs(40)):
            bad5.append(f"{ds} lab")
        if store.p100_ledger_for(ds) is None:
            bad5.append(f"{ds} P100")
    add(5, "sweeps, night lab and P100 ran every night", not bad5, ("missing: " + ", ".join(bad5[:10])) if bad5 else "all ran")
    # 6. messages
    m = set(store.alert_days("morning")) & set(dates)
    e = set(store.alert_days("evening")) & set(dates)
    test = bool(store.alert_days("test"))
    ok6 = dates and len(m) >= MESSAGE_RATE * len(dates) and len(e) >= MESSAGE_RATE * len(dates) and test
    add(6, "morning and evening messages delivered on >= 95% of days; a test alert was received", ok6,
        f"morning {len(m)}/{len(dates)}, evening {len(e)}/{len(dates)}, test alert {'sent' if test else 'never sent'}")
    # 7. unhandled
    unh = {ds: store.errors_count(d=ds, where_like="unhandled%") for ds in dates}
    add(7, "zero unhandled errors (restart with an overnight S3 position: verify by hand)",
        all(v == 0 for v in unh.values()), f"unhandled per day {list(unh.values())}")
    return {"sessions": dates, "items": items}


def format_gate_1a(res: dict) -> str:
    lines = [f"Phase 1A gate over sessions: {', '.join(res['sessions']) or '(none)'}", ""]
    for it in res["items"]:
        lines.append(f"[{'PASS' if it['pass'] else 'FAIL'}] {it['id']}. {it['name']}")
        lines.append(f"       {it['detail']}")
    ok = all(i["pass"] for i in res["items"]) and len(res["sessions"]) >= N_DAYS
    if len(res["sessions"]) < N_DAYS:
        lines.append(f"\nNOTE: {len(res['sessions'])} session(s) in the database; the gate needs {N_DAYS} consecutive.")
    lines.append("OVERALL: " + ("PASS" if ok else "FAIL"))
    lines.append("Item 7's restart test (mid-session kill, overnight S3 position adopted) is verified by hand.")
    return "\n".join(lines)
