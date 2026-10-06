"""Scheduled jobs that are not sleeves (runners for the scheduler, see scheduler.py).

NightlyJobs — on each session day, once the equity sleeves have swept (S0 at sweep_at, then S1 and
S3), in this order, one step per scheduler turn so S2's hourly cycle can interleave:

    history   store the day's 1-min bars for P100's inverse ETFs (SH, PSQ) and the lab's history
    p100      the $100 cash-account replay of the day (§8)
    recon     broker reconciliation after the equity session (§5.2)
    backup    sqlite backup to data/backups/chambers-YYYY-MM-DD.db, keep 14 (§5.3)
    evening   the evening report (§6)
    lab       start the night lab as a separate, nice'd process (§7)

Each step has a durable "done" check (a ledger row, a recon row, a file, an alert row, a lab run), so
a restart in the evening neither repeats nor skips work. A step that raises is logged and marked done
for the night so it cannot block the steps after it.

MorningBrief — 8:45 ET on session days (§6).
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timedelta
from typing import Callable, Optional

log = logging.getLogger("chambers.jobs")

SWEEP_WAIT = timedelta(minutes=45)      # start anyway if a sleeve's sweep has not appeared by sweep_at + 45 min
LATE_BRIEF = timedelta(hours=3)         # a brief missed at 8:45 (process down) is still sent until open + 3 h


class Step:
    def __init__(self, name: str, run: Callable[[date, datetime], object],
                 done: Optional[Callable[[date], bool]] = None):
        self.name, self.run, self.done = name, run, done or (lambda d: False)


class NightlyJobs:
    name = "nightly"
    state = "idle"

    def __init__(self, store, clock, steps: list[Step], sleeve_ids: list[str]):
        self.store = store
        self.clock = clock
        self.steps = steps
        self.sleeve_ids = sleeve_ids          # equity sleeves whose sweep the chain waits for
        self._done: dict[tuple[str, str], bool] = {}

    def stop(self) -> None:
        pass

    def _log(self, where: str, exc: BaseException, now: datetime) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        log.error("%s: %s", where, msg)
        self.store.log_error(where, msg, traceback.format_exc(), now)

    def swept(self, d: date) -> bool:
        return all(self.store.has_sweep_for(d, sid) for sid in self.sleeve_ids)

    def pending(self, d: date) -> list[Step]:
        out = []
        for st in self.steps:
            k = (d.isoformat(), st.name)
            if self._done.get(k):
                continue
            try:
                if st.done(d):
                    self._done[k] = True
                    continue
            except Exception:
                pass
            out.append(st)
        return out

    def plan(self, now: datetime):
        session = self.clock.today_session()
        if session is None:
            nxt = self.clock.next_session()
            return min(nxt.sweep_at if nxt else now + timedelta(hours=1), now + timedelta(hours=1)), None
        if now < session.sweep_at:
            return session.sweep_at, None
        d = session.date
        todo = self.pending(d)
        if not todo:
            nxt = self.clock.next_session()
            return min(nxt.sweep_at if nxt else now + timedelta(hours=1), now + timedelta(hours=1)), None
        if not self.swept(d) and now < session.sweep_at + SWEEP_WAIT:
            return min(now + timedelta(minutes=1), session.sweep_at + SWEEP_WAIT), None
        step = todo[0]
        return now, (lambda: self._run(step, d))

    def _run(self, step: Step, d: date) -> None:
        now = self.clock.now_et()
        log.info("nightly %s: %s", d, step.name)
        try:
            step.run(d, now)
        except Exception as e:
            self._log(f"nightly.{step.name}", e, now)
        self._done[(d.isoformat(), step.name)] = True


class MorningBrief:
    """Sends the morning brief at 8:45 ET on session days (once per day; durable via the alerts table)."""
    name = "morning"
    state = "idle"

    def __init__(self, store, clock, send: Callable[[datetime], object], at: tuple[int, int] = (8, 45)):
        self.store, self.clock, self.send, self.at = store, clock, send, at

    def stop(self) -> None:
        pass

    def _time(self, d: date) -> datetime:
        from .clock import ET
        return datetime(d.year, d.month, d.day, self.at[0], self.at[1], tzinfo=ET)

    def sent(self, d: date) -> bool:
        return any(a["ts"][:10] == d.isoformat() for a in self.store.recent_alerts(10, kind="morning")
                   if a["status"] in ("sent", "disabled", "dry_run"))

    def plan(self, now: datetime):
        session = self.clock.today_session()
        if session is not None and not self.sent(session.date):
            t = self._time(session.date)
            if now < t:
                return t, None
            if now < session.open + LATE_BRIEF:
                return now, (lambda: self.send(self.clock.now_et()))
        # Before today's open, next_session(now) is today's session, whose 8:45 may already be past (the brief
        # was sent): a past time with no action would win the scheduler's min() until 9:30 and starve S2's
        # 9:00 cycle. Ask for the session after today's open instead.
        nxt = self.clock.next_session(max(now, session.open) if session is not None else now)
        target = self._time(nxt.date) if nxt else now + timedelta(hours=1)
        return min(target, now + timedelta(hours=1)), None
