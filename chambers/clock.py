"""Market clock. All times are timezone-aware Eastern Time.

Session open/close come from Alpaca's calendar endpoint (via the broker
wrapper), cached once per day. Phase windows are derived from the
calendar's close time so holidays and early closes are handled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

ENTRIES_OPEN_DELAY = timedelta(minutes=5)
ENTRIES_CLOSE_BEFORE = timedelta(minutes=15)
FLATTEN_BEFORE = timedelta(minutes=5)
SWEEP_AFTER = timedelta(minutes=30)
PREOPEN_BEFORE = timedelta(minutes=15)


def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    """Convert an aware datetime (e.g. UTC from Alpaca) to ET. Naive input is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ET)


def seconds_until(dt: datetime, now: Optional[datetime] = None) -> float:
    now = now or now_et()
    return max(0.0, (dt - now).total_seconds())


@dataclass(frozen=True)
class Session:
    date: date
    open: datetime
    close: datetime

    @property
    def entries_open_at(self) -> datetime:
        return self.open + ENTRIES_OPEN_DELAY

    @property
    def entries_close_at(self) -> datetime:
        return self.close - ENTRIES_CLOSE_BEFORE

    @property
    def flatten_at(self) -> datetime:
        return self.close - FLATTEN_BEFORE

    @property
    def sweep_at(self) -> datetime:
        return self.close + SWEEP_AFTER

    @property
    def preopen_at(self) -> datetime:
        return self.open - PREOPEN_BEFORE

    def entries_allowed(self, now: datetime) -> bool:
        return self.entries_open_at <= now < self.entries_close_at

    def in_session(self, now: datetime) -> bool:
        return self.open <= now < self.close

    def cycle_minutes(self) -> list[datetime]:
        """Minute marks from open (inclusive) to flatten_at (exclusive) — one cycle each."""
        out, t = [], self.open
        while t < self.flatten_at:
            out.append(t)
            t += timedelta(minutes=1)
        return out


class MarketClock:
    """Calendar-aware clock. `broker.calendar(start, end)` must return a list of
    dicts {date: date, open: datetime, close: datetime} (open/close aware)."""

    def __init__(self, broker, now_fn: Optional[Callable[[], datetime]] = None):
        self._broker = broker
        self._now_fn = now_fn or now_et
        self._sessions: dict[date, Session] = {}
        self._cache_day: Optional[date] = None

    # ---- time ------------------------------------------------------------
    def now_et(self) -> datetime:
        return self._now_fn().astimezone(ET)

    def seconds_until(self, dt: datetime) -> float:
        return seconds_until(dt, self.now_et())

    # ---- calendar --------------------------------------------------------
    def _refresh(self, force: bool = False) -> None:
        today = self.now_et().date()
        if not force and self._cache_day == today and self._sessions:
            return
        rows = self._broker.calendar(today - timedelta(days=7), today + timedelta(days=14))
        sessions = {}
        for r in rows:
            sessions[r["date"]] = Session(r["date"], to_et(r["open"]), to_et(r["close"]))
        if sessions:
            self._sessions = sessions
            self._cache_day = today

    def session_for(self, d: Optional[date] = None) -> Optional[Session]:
        self._refresh()
        return self._sessions.get(d or self.now_et().date())

    def today_session(self) -> Optional[Session]:
        return self.session_for(self.now_et().date())

    def is_session_day(self, d: Optional[date] = None) -> bool:
        return self.session_for(d) is not None

    def is_market_open(self) -> bool:
        s = self.today_session()
        return bool(s and s.in_session(self.now_et()))

    def session_open(self, d: Optional[date] = None) -> Optional[datetime]:
        s = self.session_for(d)
        return s.open if s else None

    def session_close(self, d: Optional[date] = None) -> Optional[datetime]:
        s = self.session_for(d)
        return s.close if s else None

    def next_session(self, after: Optional[datetime] = None) -> Optional[Session]:
        """First session whose open is strictly after `after` (default now)."""
        self._refresh()
        after = after or self.now_et()
        future = sorted((s for s in self._sessions.values() if s.open > after), key=lambda s: s.open)
        return future[0] if future else None

    def next_session_open(self) -> Optional[datetime]:
        s = self.next_session()
        return s.open if s else None

    # ---- phase windows ----------------------------------------------------
    def entries_allowed(self) -> bool:
        s = self.today_session()
        return bool(s and s.entries_allowed(self.now_et()))

    def flatten_at(self, d: Optional[date] = None) -> Optional[datetime]:
        s = self.session_for(d)
        return s.flatten_at if s else None

    def sweep_at(self, d: Optional[date] = None) -> Optional[datetime]:
        s = self.session_for(d)
        return s.sweep_at if s else None
