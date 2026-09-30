"""History fetched from Alpaca on first run and stored (PHASE1A §2.3, §7, §8).

Only the engine process calls these (the single writer of `bars` / `bars_tf`); the night lab and
the P100 replay only read what they store. Each function fetches only what is missing, so calling it
every night is cheap after the first time.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from .bars import TF_1D, TF_1H, TF_S4H, build_session_4h
from .clock import ET, MarketClock, Session
from .replay import default_session
from .store import Bar, Store

log = logging.getLogger("chambers.history")


def recent_sessions(clock: Optional[MarketClock], before: date, n: int) -> list[Session]:
    """The last `n` sessions strictly before `before`, from the calendar (weekdays 9:30–16:00 without one)."""
    if n <= 0:
        return []
    span = timedelta(days=int(n * 1.5) + 10)
    if clock is not None:
        try:
            ss = clock.sessions_between(before - span, before - timedelta(days=1))
            out = [ss[d] for d in sorted(ss) if d < before]
            return out[-n:]
        except Exception as e:
            log.warning("calendar unavailable (%s); assuming weekday sessions", e)
    out, d = [], before - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(default_session(d))
        d -= timedelta(days=1)
    return out[::-1]


def ensure_1m(store: Store, broker, clock, symbols: list[str], n_sessions: int, before: date) -> int:
    """1-minute bars for the last `n_sessions` sessions before `before`, for every symbol. Returns bars written."""
    sessions = recent_sessions(clock, before, n_sessions)
    missing = [s for s in sessions if any(store.bar_count_for_day(s.date, sym) == 0 for sym in symbols)]
    if not missing:
        return 0
    fetched = broker.bars_1m(symbols, missing[0].open, missing[-1].close)
    bounds = {s.date: s for s in missing}
    n = 0
    for sym, bars in fetched.items():
        keep = [b for b in bars if b.ts.date() in bounds and bounds[b.ts.date()].open <= b.ts < bounds[b.ts.date()].close]
        n += store.write_bars(sym, keep)
    log.info("history: %d 1-min bars for %s over %d sessions", n, symbols, len(missing))
    return n


def ensure_s4h(store: Store, broker, clock, symbols: list[str], n_sessions: int, before: date) -> int:
    """Session-4h bars (S3) for the last `n_sessions` sessions, built from 30-minute bars."""
    sessions = recent_sessions(clock, before, n_sessions)
    if not sessions:
        return 0
    have = {sym: {b.ts.date() for b in store.bars_tf(sym, TF_S4H, sessions[0].open, before_dt(before))}
            for sym in symbols}
    missing = [s for s in sessions if any(s.date not in have[sym] for sym in symbols)]
    if not missing:
        return 0
    fetched = broker.bars(symbols, missing[0].open, missing[-1].close, 30)
    n = 0
    for sym, bars in fetched.items():
        by_day: dict[date, list[Bar]] = {}
        for b in bars:
            by_day.setdefault(b.ts.date(), []).append(b)
        out = []
        for s in missing:
            out += build_session_4h(by_day.get(s.date, []), s)
        n += store.write_bars_tf(sym, TF_S4H, out)
    log.info("history: %d session-4h bars for %s over %d sessions", n, symbols, len(missing))
    return n


def before_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def ensure_crypto_1h(store: Store, broker, symbols: list[str], days: int, now: datetime) -> int:
    """1-hour crypto bars (S2) for the last `days` days; only complete hours are stored."""
    n = 0
    for sym in symbols:
        last = store.last_bar_tf_ts(sym, TF_1H)
        start = (last + timedelta(hours=1)) if last else now - timedelta(days=days)
        if start + timedelta(hours=1) > now:
            continue
        fetched = broker.crypto_bars([sym], start, now, 60).get(sym, [])
        keep = [b for b in fetched if b.ts + timedelta(hours=1) <= now]
        n += store.write_bars_tf(sym, TF_1H, keep)
    return n


def ensure_daily(store: Store, broker, symbols: list[str], n_days: int, now: datetime) -> int:
    """Daily bars (lab L2's 200-session SMA, morning brief) for about `n_days` sessions."""
    n = 0
    for sym in symbols:
        last = store.last_bar_tf_ts(sym, TF_1D)
        start = (last + timedelta(days=1)) if last else now - timedelta(days=int(n_days * 1.5) + 10)
        if start.date() >= now.date():
            continue
        fetched = broker.bars([sym], start, before_dt(now.date()), 1440).get(sym, [])
        n += store.write_bars_tf(sym, TF_1D, [b for b in fetched if b.ts.date() < now.date()])
    return n
