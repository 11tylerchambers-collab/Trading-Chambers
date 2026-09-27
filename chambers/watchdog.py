"""Heartbeat watchdog (PHASE1A §6: "heartbeat stale > 3 min during any sleeve's active hours").

Run every minute by `deploy/chambers-watchdog.timer` as its own process (`--watchdog`), so it can report
an engine that is hung or not running at all, which a check inside the engine cannot. A sleeve is
stale when a cycle it should have run is more than 3 minutes overdue, measured on that sleeve's own
schedule: S0 every minute from open to flatten_at, S1 each 15-minute mark, S2 every hour (24/7), S3
at 13:30:05 and flatten_at. `expected_marks` is also what `--gate-1a` counts cycles against.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .bars import s1_decision_times, s4h_decision_times
from .clock import ET, Session

STALE = timedelta(minutes=3)
UTC = timezone.utc


def expected_marks(sleeve_id: str, session: Optional[Session], start: datetime, end: datetime) -> list[datetime]:
    """Cycle times the sleeve should have run in [start, end)."""
    if sleeve_id == "S2":
        t = start.astimezone(UTC).replace(minute=0, second=10, microsecond=0)
        if t < start:
            t += timedelta(hours=1)
        out = []
        while t < end:
            out.append(t.astimezone(ET))
            t += timedelta(hours=1)
        return out
    if session is None:
        return []
    if sleeve_id == "S0":
        marks = [m + timedelta(seconds=5) for m in session.cycle_minutes()]
    elif sleeve_id == "S1":
        marks = s1_decision_times(session)
    else:
        marks = s4h_decision_times(session)
    return [m for m in marks if start <= m < end]


def stale_sleeves(store, clock, sleeve_ids: list[str], now: datetime) -> list[dict]:
    """Sleeves whose latest expected cycle (≥ 3 min ago) has no cycle row at or after it."""
    session = clock.today_session()
    out = []
    for sid in sleeve_ids:
        window_start = now - timedelta(hours=2)
        marks = expected_marks(sid, session, window_start, now - STALE)
        if not marks:
            continue
        due = marks[-1]
        hb = store.read_heartbeat(sid) or {}
        last = datetime.fromisoformat(hb["last_cycle_ts"]) if hb.get("last_cycle_ts") else None
        if last is None or last < due - timedelta(seconds=5):
            out.append({"sleeve_id": sid, "due": due, "last_cycle_ts": last,
                        "overdue_s": int((now - due).total_seconds())})
    return out


def run_watchdog(store, clock, notifier, sleeve_ids: list[str], now: Optional[datetime] = None) -> list[dict]:
    now = now or clock.now_et()
    stale = stale_sleeves(store, clock, sleeve_ids, now)
    for s in stale:
        last = s["last_cycle_ts"].strftime("%H:%M:%S") if s["last_cycle_ts"] else "never"
        notifier.alert(f"stale:{s['sleeve_id']}", (
            f"HEARTBEAT STALE: {s['sleeve_id']} missed its {s['due'].strftime('%H:%M:%S')} ET cycle "
            f"({s['overdue_s'] // 60} min overdue; last cycle {last}). Check the engine."), now)
    return stale
