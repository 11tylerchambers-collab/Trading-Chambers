from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from chambers.clock import ET, MarketClock, Session, seconds_until, to_et

UTC = ZoneInfo("UTC")


class FakeCalendarBroker:
    """Two sessions: a normal day and an early-close day (13:00). Returns times in UTC like Alpaca."""

    def __init__(self):
        self.calls = 0

    def calendar(self, start, end):
        self.calls += 1
        rows = []
        for d, close_h in ((date(2026, 11, 25), 16), (date(2026, 11, 27), 13)):
            if start <= d <= end:
                o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(UTC)
                c = datetime(d.year, d.month, d.day, close_h, 0, tzinfo=ET).astimezone(UTC)
                rows.append({"date": d, "open": o, "close": c})
        return rows


def make(now):
    b = FakeCalendarBroker()
    return MarketClock(b, now_fn=lambda: now), b


def test_to_et_converts_utc_and_naive():
    utc = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    assert to_et(utc) == datetime(2026, 9, 22, 10, 0, tzinfo=ET)
    assert to_et(datetime(2026, 9, 22, 14, 0)) == datetime(2026, 9, 22, 10, 0, tzinfo=ET)


def test_session_windows_normal_day():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=ET)
    clk, _ = make(now)
    s = clk.today_session()
    assert s.open == datetime(2026, 11, 25, 9, 30, tzinfo=ET)
    assert s.close == datetime(2026, 11, 25, 16, 0, tzinfo=ET)
    assert s.entries_open_at == datetime(2026, 11, 25, 9, 35, tzinfo=ET)
    assert s.entries_close_at == datetime(2026, 11, 25, 15, 45, tzinfo=ET)
    assert s.flatten_at == datetime(2026, 11, 25, 15, 55, tzinfo=ET)
    assert s.sweep_at == datetime(2026, 11, 25, 16, 30, tzinfo=ET)
    assert clk.is_market_open() and clk.entries_allowed()
    assert len(s.cycle_minutes()) == 385  # 9:30 .. 15:54 inclusive


def test_early_close_windows():
    d = date(2026, 11, 27)
    clk, _ = make(datetime(2026, 11, 27, 12, 50, tzinfo=ET))
    s = clk.session_for(d)
    assert s.close == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert s.entries_close_at == datetime(2026, 11, 27, 12, 45, tzinfo=ET)
    assert clk.flatten_at(d) == datetime(2026, 11, 27, 12, 55, tzinfo=ET)
    assert clk.sweep_at(d) == datetime(2026, 11, 27, 13, 30, tzinfo=ET)
    # 12:50 is inside the session but entries are closed (C - 15min = 12:45)
    assert clk.is_market_open()
    assert not clk.entries_allowed()
    # boundaries
    assert s.entries_allowed(datetime(2026, 11, 27, 9, 35, tzinfo=ET))
    assert not s.entries_allowed(datetime(2026, 11, 27, 9, 34, 59, tzinfo=ET))
    assert not s.entries_allowed(datetime(2026, 11, 27, 12, 45, tzinfo=ET))
    assert s.entries_allowed(datetime(2026, 11, 27, 12, 44, 59, tzinfo=ET))


def test_non_session_day_and_next_open():
    # Thanksgiving 2026-11-26 is not in the calendar
    clk, _ = make(datetime(2026, 11, 26, 11, 0, tzinfo=ET))
    assert clk.today_session() is None
    assert not clk.is_market_open() and not clk.entries_allowed()
    assert clk.session_open() is None and clk.flatten_at() is None
    assert clk.next_session_open() == datetime(2026, 11, 27, 9, 30, tzinfo=ET)


def test_next_session_after_close_is_tomorrow():
    clk, _ = make(datetime(2026, 11, 25, 16, 30, tzinfo=ET))
    assert not clk.is_market_open()
    assert clk.next_session_open() == datetime(2026, 11, 27, 9, 30, tzinfo=ET)


def test_calendar_cached_per_day():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=ET)
    clk, b = make(now)
    clk.today_session(); clk.is_market_open(); clk.next_session_open()
    assert b.calls == 1


def test_seconds_until_clamps_to_zero():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=ET)
    assert seconds_until(now + timedelta(seconds=90), now) == 90
    assert seconds_until(now - timedelta(seconds=90), now) == 0
