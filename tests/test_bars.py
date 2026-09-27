"""Bar builders (15-min, session-4h, 1h, 5-min, session) incl. an early close, and indicators."""
from datetime import date, datetime, timedelta, timezone

import pytest

from chambers.bars import (atr, avg_volume, build_1h, build_15m, build_5m, build_session_4h, build_session_bar,
                           ema, ema_series, highest_high, lowest_low, s1_decision_times, s4h_decision_times, sma,
                           stdev)
from chambers.clock import ET, Session
from chambers.store import Bar

D = date(2026, 9, 22)
REG = Session(D, datetime(2026, 9, 22, 9, 30, tzinfo=ET), datetime(2026, 9, 22, 16, 0, tzinfo=ET))
EARLY = Session(date(2026, 11, 27), datetime(2026, 11, 27, 9, 30, tzinfo=ET), datetime(2026, 11, 27, 13, 0, tzinfo=ET))


def minutes(session, n=None, price=lambda i: 100.0 + i * 0.01, vol=lambda i: 10.0):
    n = n if n is not None else int((session.close - session.open).total_seconds() // 60)
    out = []
    for i in range(n):
        p = price(i)
        out.append(Bar(session.open + timedelta(minutes=i), p, p + 0.05, p - 0.05, p + 0.01, vol(i)))
    return out


def test_15m_session_aligned_full_day():
    bars = build_15m(minutes(REG), REG)
    assert len(bars) == 26
    assert bars[0].ts == REG.open and bars[-1].ts == datetime(2026, 9, 22, 15, 45, tzinfo=ET)
    one = minutes(REG)[:15]
    assert bars[0].o == one[0].o and bars[0].c == one[-1].c and bars[0].v == 150
    assert bars[0].h == max(b.h for b in one) and bars[0].l == min(b.l for b in one)


def test_15m_only_complete_buckets_before_cutoff():
    bars = build_15m(minutes(REG, 50), REG, cutoff=datetime(2026, 9, 22, 10, 15, tzinfo=ET))
    assert [b.ts.strftime("%H:%M") for b in bars] == ["09:30", "09:45", "10:00"]
    # 10:15-10:20 has data but the bucket has not ended
    bars = build_15m(minutes(REG, 50), REG, cutoff=datetime(2026, 9, 22, 10, 14, 59, tzinfo=ET))
    assert len(bars) == 2


def test_15m_early_close_truncates_last_bucket():
    bars = build_15m(minutes(EARLY), EARLY)
    assert len(bars) == 14                                 # 9:30 .. 12:45, 3.5 h
    assert bars[-1].ts == datetime(2026, 11, 27, 12, 45, tzinfo=ET)
    times = s1_decision_times(EARLY)
    assert times[0] == datetime(2026, 11, 27, 9, 45, 5, tzinfo=ET)
    assert times[-1] == datetime(2026, 11, 27, 12, 45, 5, tzinfo=ET)   # flatten_at 12:55; 13:00:05 excluded
    reg = s1_decision_times(REG)
    assert len(reg) == 25 and reg[-1] == datetime(2026, 9, 22, 15, 45, 5, tzinfo=ET)


def test_15m_missing_minutes_and_empty_bucket():
    ms = [b for b in minutes(REG, 60) if not (15 <= (b.ts - REG.open).seconds // 60 < 30)]
    bars = build_15m(ms, REG, cutoff=REG.open + timedelta(minutes=60))
    assert [b.ts.strftime("%H:%M") for b in bars] == ["09:30", "10:00", "10:15"]   # 9:45 bucket had no prints


def test_session_4h_regular_day():
    bars = build_session_4h(minutes(REG), REG)
    assert len(bars) == 2
    assert bars[0].ts == REG.open and bars[1].ts == datetime(2026, 9, 22, 13, 30, tzinfo=ET)
    assert bars[0].v == 240 * 10 and bars[1].v == 150 * 10
    assert s4h_decision_times(REG) == [datetime(2026, 9, 22, 13, 30, 5, tzinfo=ET), REG.flatten_at]


def test_session_4h_early_close_is_one_bar():
    bars = build_session_4h(minutes(EARLY), EARLY)
    assert len(bars) == 1 and bars[0].ts == EARLY.open and bars[0].v == 210 * 10
    assert s4h_decision_times(EARLY) == [EARLY.flatten_at]   # 12:55, the only decision


def test_session_4h_partial_last_bar_at_flatten():
    ms = minutes(REG)
    cut = REG.flatten_at
    full = build_session_4h(ms, REG, cutoff=cut)
    assert len(full) == 1                                   # the 13:30-16:00 bar has not ended
    part = build_session_4h(ms, REG, cutoff=cut, partial_last=True)
    assert len(part) == 2 and part[1].ts == datetime(2026, 9, 22, 13, 30, tzinfo=ET)
    assert part[1].v == (385 - 240) * 10                    # 13:30 .. 15:54 inclusive
    assert part[1].c == [b for b in ms if b.ts < cut][-1].c


def test_session_4h_from_30_minute_bars():
    thirty = [Bar(REG.open + timedelta(minutes=30 * i), 1, 2, 0.5, 1.5, 100) for i in range(13)]
    bars = build_session_4h(thirty, REG)
    assert [b.v for b in bars] == [800, 500]


def test_1h_across_midnight_utc():
    start = datetime(2026, 9, 22, 23, 0, tzinfo=timezone.utc)
    ms = [Bar(start + timedelta(minutes=i), 1, 1, 1, 1, 1) for i in range(150)]   # 23:00Z .. 01:29Z
    bars = build_1h(ms, cutoff=datetime(2026, 9, 23, 1, 30, tzinfo=timezone.utc))
    assert [b.ts.astimezone(timezone.utc).strftime("%d %H") for b in bars] == ["22 23", "23 00"]
    assert all(b.v == 60 for b in bars)


def test_5m_and_session_bar():
    assert len(build_5m(minutes(REG), REG)) == 78
    sb = build_session_bar(minutes(REG), REG)
    assert len(sb) == 1 and sb[0].v == 3900


def test_indicators():
    assert sma([1, 2, 3, 4], 2) == 3.5 and sma([1], 2) is None
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9], 8) == pytest.approx(2.0)
    bars = [Bar(datetime(2026, 1, 1, tzinfo=ET) + timedelta(hours=i), 10, 11 + (i % 2), 9, 10, 100 + i) for i in range(16)]
    # true ranges after the first: h - l = 2 or 3; |h - prev c| ≤ 2 → TR = 2, 3, 2, 3 ...
    assert atr(bars, 14) == pytest.approx((7 * 2 + 7 * 3) / 14)
    assert atr(bars[:14], 14) is None
    assert highest_high(bars, 3) == 12 and lowest_low(bars, 3) == 9
    assert avg_volume(bars, 2) == pytest.approx((113 + 114) / 2)
    s = ema_series([1, 2, 3, 4, 5], 3)
    assert s[:2] == [None, None] and s[2] == 2.0 and s[3] == pytest.approx(3.0) and ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)
