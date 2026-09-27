"""Bar builders (PHASE1A §2) and the indicators the Phase 1A strategies use.

All builders take bars in time order whose `ts` is the bar START (as Alpaca and the store keep them)
and return aggregated bars, also stamped with their start. A bucket is emitted only when it is
complete: its end is at or before `cutoff` (default: the session close). A bucket with no input bars
(no prints on IEX that minute range) produces no bar.

- 15-minute: session-aligned from the open (9:30–9:45 is the first); the last bucket is truncated at
  the close, so a 13:00 early close ends with 12:45–13:00.
- Session 4-hour: two bars per session, open → open+4h and open+4h → close; on an early close
  (open+4h ≥ close) one bar, open → close. `partial_last=True` emits the last bucket from whatever
  input exists before `cutoff` — S3 decides on the last bar at flatten_at, while the session is still
  open, because an order after the close could not fill until the next open.
- 1-hour: clock hours (UTC and ET hours share boundaries), for 24/7 crypto.
- 5-minute and session (daily) bars for the night lab.

Input may be any bar size that does not straddle a bucket boundary (1-min, or 30-min for S3 history).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from .clock import Session
from .store import Bar

TF_15M = "15m"
TF_1H = "1h"
TF_S4H = "s4h"
TF_1D = "1d"


def aggregate(bars: Sequence[Bar], start: datetime) -> Bar:
    return Bar(start, bars[0].o, max(b.h for b in bars), min(b.l for b in bars), bars[-1].c, sum(b.v for b in bars))


def _bucketize(bars: Iterable[Bar], buckets: list[tuple[datetime, datetime]], cutoff: datetime,
               partial_last: bool = False) -> list[Bar]:
    bars = sorted(bars, key=lambda b: b.ts)
    out = []
    i = 0
    for k, (start, end) in enumerate(buckets):
        last = k == len(buckets) - 1
        complete = end <= cutoff
        if not complete and not (partial_last and last and start < cutoff):
            continue
        limit = end if complete else cutoff
        while i < len(bars) and bars[i].ts < start:
            i += 1
        j = i
        while j < len(bars) and bars[j].ts < limit:
            j += 1
        if j > i:
            out.append(aggregate(bars[i:j], start))
        i = j
    return out


def buckets_15m(session: Session) -> list[tuple[datetime, datetime]]:
    out, t = [], session.open
    while t < session.close:
        out.append((t, min(t + timedelta(minutes=15), session.close)))
        t += timedelta(minutes=15)
    return out


def buckets_minutes(session: Session, minutes: int) -> list[tuple[datetime, datetime]]:
    out, t = [], session.open
    while t < session.close:
        out.append((t, min(t + timedelta(minutes=minutes), session.close)))
        t += timedelta(minutes=minutes)
    return out


def buckets_s4h(session: Session) -> list[tuple[datetime, datetime]]:
    mid = session.open + timedelta(hours=4)
    if mid >= session.close:
        return [(session.open, session.close)]
    return [(session.open, mid), (mid, session.close)]


def build_15m(bars_1m: Iterable[Bar], session: Session, cutoff: Optional[datetime] = None) -> list[Bar]:
    return _bucketize(bars_1m, buckets_15m(session), cutoff or session.close)


def build_5m(bars_1m: Iterable[Bar], session: Session, cutoff: Optional[datetime] = None) -> list[Bar]:
    return _bucketize(bars_1m, buckets_minutes(session, 5), cutoff or session.close)


def build_session_4h(bars: Iterable[Bar], session: Session, cutoff: Optional[datetime] = None,
                     partial_last: bool = False) -> list[Bar]:
    return _bucketize(bars, buckets_s4h(session), cutoff or session.close, partial_last)


def build_session_bar(bars: Iterable[Bar], session: Session) -> list[Bar]:
    return _bucketize(bars, [(session.open, session.close)], session.close)


def build_1h(bars_1m: Iterable[Bar], cutoff: datetime) -> list[Bar]:
    """Clock-hour bars from 1-minute bars; only hours that ended at or before `cutoff`."""
    by_hour: dict[datetime, list[Bar]] = {}
    for b in sorted(bars_1m, key=lambda x: x.ts):
        by_hour.setdefault(b.ts.replace(minute=0, second=0, microsecond=0), []).append(b)
    return [aggregate(bs, h) for h, bs in sorted(by_hour.items()) if h + timedelta(hours=1) <= cutoff]


def s4h_decision_times(session: Session) -> list[datetime]:
    """When S3 decides: at the end of every non-last session-4h bar (+5 s, bars through the minute before),
    and at flatten_at for the last bar (see module docstring)."""
    buckets = buckets_s4h(session)
    out = [end + timedelta(seconds=5) for _, end in buckets[:-1] if end + timedelta(seconds=5) < session.flatten_at]
    return out + [session.flatten_at]


def s1_decision_times(session: Session) -> list[datetime]:
    """S1 cycles: 5 s after each 15-minute bucket ends, while before flatten_at."""
    return [end + timedelta(seconds=5) for _, end in buckets_15m(session)
            if end + timedelta(seconds=5) < session.flatten_at]


# --------------------------------------------------------------------------
# indicators (pure, on lists; None until enough data)
# --------------------------------------------------------------------------

def sma(values: Sequence[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def stdev(values: Sequence[float], n: int) -> Optional[float]:
    """Population standard deviation of the last n values (the Bollinger convention)."""
    if n <= 1 or len(values) < n:
        return None
    w = values[-n:]
    m = sum(w) / n
    return math.sqrt(sum((x - m) ** 2 for x in w) / n)


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            out.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
    """Simple average of the last n true ranges (needs n + 1 bars so every TR has a previous close)."""
    if len(bars) < n + 1:
        return None
    tr = true_ranges(bars[-(n + 1):])[1:]
    return sum(tr) / n


def ema_series(values: Sequence[float], n: int) -> list[Optional[float]]:
    """EMA over the whole series, seeded with the SMA of the first n values; None before that."""
    out: list[Optional[float]] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def ema(values: Sequence[float], n: int) -> Optional[float]:
    s = ema_series(values, n)
    return s[-1] if s else None


def highest_high(bars: Sequence[Bar], n: int, exclude_last: bool = True) -> Optional[float]:
    w = bars[:-1] if exclude_last else bars
    if len(w) < n:
        return None
    return max(b.h for b in w[-n:])


def lowest_low(bars: Sequence[Bar], n: int, exclude_last: bool = True) -> Optional[float]:
    w = bars[:-1] if exclude_last else bars
    if len(w) < n:
        return None
    return min(b.l for b in w[-n:])


def avg_volume(bars: Sequence[Bar], n: int, exclude_last: bool = True) -> Optional[float]:
    w = bars[:-1] if exclude_last else bars
    if len(w) < n:
        return None
    return sum(b.v for b in w[-n:]) / n
