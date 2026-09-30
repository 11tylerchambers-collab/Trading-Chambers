"""Synthetic stored history for the night lab and P100 tests (seeded, deterministic)."""
import math
import random
from datetime import date, datetime, timedelta

from chambers.bars import TF_1D
from chambers.clock import ET
from chambers.store import Bar


def weekdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def minute_day(d: date, start: float, rng: random.Random, drift: float = 0.0, vol: float = 0.0006,
               n: int = 390, base_volume: float = 1000.0):
    o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    px, out = start, []
    for i in range(n):
        step = rng.gauss(drift if i >= 60 else 0.0, vol)
        # mean-reverting wiggles so VWAP strategies fire, plus the drift after the first hour
        px = px * (1 + step) - (px - start) * 0.002
        c = round(px, 4)
        v = base_volume * (3.0 if rng.random() < 0.05 else 1.0)
        out.append(Bar(o + timedelta(minutes=i), c, c * 1.0004, c * 0.9996, c, v))
    return out, px


def fill_store(st, end: date = date(2026, 9, 25), sessions: int = 44, symbols=("SPY", "QQQ"),
               s0_symbols=("AAPL", "MSFT", "SPY", "QQQ"), extra=("SH", "PSQ"), seed: int = 7, daily_days: int = 260):
    rng = random.Random(seed)
    days = weekdays(end, sessions)
    px = {s: 100.0 + 50 * k for k, s in enumerate(sorted(set(symbols) | set(s0_symbols) | set(extra)))}
    for d in days:
        trend = rng.choice([0.0, 0.0, 0.00012, -0.00012])
        for s in sorted(set(symbols) | set(s0_symbols)):
            bars, px[s] = minute_day(d, px[s], rng, drift=trend)
            st.write_bars(s, bars)
        spy = st.bars_for_day(d, symbol="SPY")["SPY"]
        qqq = st.bars_for_day(d, symbol="QQQ")["QQQ"]
        for inv, src in (("SH", spy), ("PSQ", qqq)):
            if inv in extra:
                base = px[inv]
                st.write_bars(inv, [Bar(b.ts, base * src[0].c / b.c, base * src[0].c / b.c, base * src[0].c / b.c,
                                        base * src[0].c / b.c, 500.0) for b in src])
    # daily bars: a long uptrend with regular 3-4 day pullbacks (L2)
    dd = weekdays(end, daily_days)
    for k, s in enumerate(symbols):
        p, out = 300.0 + 100 * k, []
        for i, d in enumerate(dd):
            p *= 1 + (0.004 if i % 7 < 4 else -0.004)
            out.append(Bar(datetime(d.year, d.month, d.day, tzinfo=ET), p, p * 1.005, p * 0.995, p, 1e6))
        st.write_bars_tf(s, TF_1D, out)
    return days
