"""Random twins (PHASE1A §4).

Every sleeve gets a twin: same symbols, same bars, same exits, same sizing, but random entries at
the sleeve's own average entry frequency, random direction where the sleeve may go both ways.
Simulated only — no orders — costed like replay (fixed 0.02% spread + slippage, + the crypto fee
for S2), stored in `twin_trades`.

Reproducibility: the seed is fixed per sleeve per day (sha256 of "sleeve|day") and logged in
`twin_seeds` with the entry probability p. Every draw uses its own RNG seeded with
"seed|symbol|bar timestamp", so the same seed, p and bars give the same twin trades no matter how
the day was split into cycles, restarted, or replayed (`TwinBook` runs identically in both).

p = entries / eligible evaluations of the real sleeve over its trailing 20 sessions before the day
(from `signals`; an evaluation is eligible unless entries were closed, bars were insufficient, the
sleeve already held the symbol or was cooling down). With no history yet, p comes from a replay of
the most recent stored data at the current params (`p_source` says which). The twin draws once per
new bar per symbol when it is flat on that symbol and the sleeve could have entered.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

from .engine import Position, check_exit, trade_economics
from .replay import ReplayBroker
from .store import iso, parse_ts

log = logging.getLogger("chambers.twin")

TRAILING_SESSIONS = 20
NOT_ELIGIBLE = ("entries_closed", "insufficient_bars", "already_in_position", "cooldown")


def day_seed(sleeve_id: str, day: str) -> int:
    return int(hashlib.sha256(f"{sleeve_id}|{day}".encode()).hexdigest()[:8], 16)


def draw(seed: int, symbol: str, bar_ts: datetime) -> tuple[float, float]:
    """(u for 'enter?', v for direction) — one independent RNG per (seed, symbol, bar)."""
    rng = random.Random(f"{seed}|{symbol}|{iso(bar_ts)}")
    return rng.random(), rng.random()


@dataclass
class TwinCtx:
    """What the twin sees for one symbol on one cycle."""
    bar_ts: datetime
    close: float
    eligible: bool                                   # enough data for the sleeve to have evaluated an entry
    exit: Callable[[Position], Optional[str]]        # the sleeve's own exit rule
    size: Callable[[str, float], float]              # the sleeve's own sizing (side, price) -> qty
    stop: Callable[[str, float], Optional[float]] = lambda side, price: None


@dataclass
class TwinTrade:
    symbol: str
    side: str
    qty: float
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    bars_held: int
    mae_pct: float
    mfe_pct: float
    gross_pnl: float
    est_cost: float
    net_pnl: float
    seed: int
    day: str


class TwinBook:
    """The twin's positions and trades. `store=None` for replay (trades kept in `self.trades`)."""

    def __init__(self, sleeve_id: str, allow_short: bool, fee_rate: float = 0.0, store=None,
                 news: Optional[Callable[[date], tuple[bool, Optional[str]]]] = None):
        self.sleeve_id = sleeve_id
        self.allow_short = allow_short
        self.fee_rate = fee_rate
        self.store = store
        self.news = news or (lambda d: (False, None))
        self.broker = ReplayBroker()
        self.positions: dict[str, Position] = {}
        self.trades: list[TwinTrade] = []
        self.last_draw: dict[str, datetime] = {}
        self.day: Optional[str] = None
        self.seed: int = 0
        self.p: float = 0.0

    # ---- day / seed ------------------------------------------------------------
    def set_day(self, day: str, seed: int, p: float) -> None:
        self.day, self.seed, self.p = day, seed, p

    # ---- the step ----------------------------------------------------------------
    def step(self, t: datetime, ctxs: dict[str, TwinCtx], entries_allowed: bool) -> None:
        for sym, pos in list(self.positions.items()):
            c = ctxs.get(sym)
            if c is None:
                continue
            if pos.on_bar(c.bar_ts, c.close):
                self._progress(pos)
            reason = c.exit(pos)
            if reason:
                self.close(pos, c.close, t, reason, c.bar_ts)
        if not entries_allowed or self.p <= 0:
            return
        for sym, c in ctxs.items():
            if sym in self.positions or not c.eligible:
                continue
            last = self.last_draw.get(sym)
            if last is not None and c.bar_ts <= last:
                continue
            self.last_draw[sym] = c.bar_ts
            u, v = draw(self.seed, sym, c.bar_ts)
            if u >= self.p:
                continue
            side = "short" if self.allow_short and v >= 0.5 else "long"
            qty = c.size(side, c.close)
            if not qty or qty <= 0:
                continue
            self.open(sym, side, qty, c.close, t, c.bar_ts, c.stop(side, c.close))

    def open(self, sym: str, side: str, qty: float, price: float, t: datetime, bar_ts: datetime,
             stop_price: Optional[float]) -> None:
        bid, ask = self.broker.quote(price)
        pos = Position(None, sym, side, qty, price, t, bar_ts, entry_bid=bid, entry_ask=ask, stop_price=stop_price,
                       extra={"day": self.day, "seed": self.seed, "entry_bar_ts": iso(bar_ts)})
        if self.store is not None:
            nd, ev = self.news(t.date())
            pos.trade_id = self.store.open_twin_trade(
                self.sleeve_id, self.day, self.seed, sym, side, qty, t, price,
                {"random_entry": True, "p": round(self.p, 6), "seed": self.seed, "stop_price": stop_price},
                self._state(pos), nd, ev)
        self.positions[sym] = pos

    def _state(self, pos: Position) -> dict:
        return {"last_bar_ts": iso(pos.last_bar_ts) if pos.last_bar_ts else None, "stop_price": pos.stop_price,
                "entry_bar_ts": pos.extra.get("entry_bar_ts"), "entry_bid": pos.entry_bid, "entry_ask": pos.entry_ask}

    def _progress(self, pos: Position) -> None:
        if self.store is not None and pos.trade_id is not None:
            self.store.update_twin_progress(pos.trade_id, pos.bars_held, pos.mae_pct, pos.mfe_pct, self._state(pos))

    def close(self, pos: Position, price: float, t: datetime, reason: str, bar_ts: Optional[datetime] = None) -> None:
        bid, ask = self.broker.quote(price)
        pos.update_excursion(price)
        gross, cost, net = trade_economics(pos.side, pos.qty, pos.entry_price, price, pos.entry_bid, pos.entry_ask,
                                           bid, ask, self.fee_rate)
        tr = TwinTrade(pos.symbol, pos.side, pos.qty, pos.entry_ts, pos.entry_price, t, price, reason, pos.bars_held,
                       pos.mae_pct, pos.mfe_pct, gross, cost, net, pos.extra.get("seed", self.seed),
                       pos.extra.get("day", self.day))
        self.trades.append(tr)
        if self.store is not None and pos.trade_id is not None:
            self.store.close_twin_trade(pos.trade_id, t, price, reason, pos.bars_held, pos.mae_pct, pos.mfe_pct,
                                        gross, cost, net)
        self.positions.pop(pos.symbol, None)
        if bar_ts is not None:
            self.last_draw[pos.symbol] = max(bar_ts, self.last_draw.get(pos.symbol, bar_ts))

    def flatten(self, t: datetime, prices: dict[str, float], reason: str = "eod_flatten") -> None:
        for sym, pos in list(self.positions.items()):
            self.close(pos, prices.get(sym) or pos.entry_price, t, reason)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    # ---- restart ---------------------------------------------------------------
    def restore(self) -> None:
        """Re-open the twin positions that were open when the process stopped."""
        if self.store is None:
            return
        self.positions = {}
        for r in self.store.open_twin_trades(self.sleeve_id):
            st = json.loads(r["state_json"] or "{}")
            pos = Position(r["id"], r["symbol"], r["side"], r["qty"], r["entry_price"], parse_ts(r["entry_ts"]),
                           parse_ts(st["last_bar_ts"]) if st.get("last_bar_ts") else None,
                           bars_held=r["bars_held"], mae_pct=r["mae_pct"], mfe_pct=r["mfe_pct"],
                           entry_bid=st.get("entry_bid"), entry_ask=st.get("entry_ask"),
                           stop_price=st.get("stop_price"),
                           extra={"day": r["day"], "seed": r["seed"], "entry_bar_ts": st.get("entry_bar_ts")})
            self.positions[r["symbol"]] = pos
            if pos.last_bar_ts is not None:
                self.last_draw[r["symbol"]] = pos.last_bar_ts


def trailing_rate(store, sleeve_id: str, day: str) -> tuple[float, int, int]:
    """(p, fired, eligible) over the sleeve's trailing 20 signal days before `day`."""
    dates = store.signal_dates(sleeve_id, day, TRAILING_SESSIONS)
    fired, eligible = store.signal_rate(sleeve_id, dates, NOT_ELIGIBLE)
    return (fired / eligible if eligible else 0.0), fired, eligible


class LiveTwin:
    """A sleeve's twin inside the live engine: picks the day's seed/p, persists trades, restores on restart."""

    def __init__(self, sleeve, allow_short: bool, fee_rate: float = 0.0,
                 fallback_rate: Optional[Callable[[], Optional[float]]] = None,
                 day_fn: Optional[Callable[[datetime], str]] = None):
        self.sleeve = sleeve
        self.book = TwinBook(sleeve.sleeve_id, allow_short, fee_rate, store=sleeve.store, news=sleeve.news)
        self.fallback_rate = fallback_rate
        self.day_fn = day_fn or (lambda now: now.date().isoformat())

    def ensure_day(self, now: datetime) -> None:
        day = self.day_fn(now)
        if self.book.day == day:
            return
        store = self.sleeve.store
        row = store.get_twin_seed(self.sleeve.sleeve_id, day)
        if row is None:
            p, fired, eligible = trailing_rate(store, self.sleeve.sleeve_id, day)
            source = f"trailing: {fired} entries / {eligible} eligible evaluations"
            if eligible == 0 and self.fallback_rate is not None:
                try:
                    fb = self.fallback_rate()
                except Exception as e:  # never let the twin stop a cycle
                    log.warning("%s twin fallback rate failed: %s", self.sleeve.sleeve_id, e)
                    fb = None
                if fb is not None:
                    p, source = fb, "replay of recent stored data at current params (no live history yet)"
            store.set_twin_seed(self.sleeve.sleeve_id, day, day_seed(self.sleeve.sleeve_id, day), p, source, now)
            row = store.get_twin_seed(self.sleeve.sleeve_id, day)
        self.book.set_day(day, row["seed"], row["p"])
        log.info("%s twin day %s seed=%d p=%.5f (%s)", self.sleeve.sleeve_id, day, row["seed"], row["p"],
                 row["p_source"])

    def restore(self, now: datetime) -> None:
        self.book.restore()
        self.ensure_day(now)

    # ---- S0 ------------------------------------------------------------------
    def on_cycle_s0(self, eng, now: datetime, session, entries_ok: bool) -> None:
        self.ensure_day(now)
        p = eng.params
        ctxs = {}
        for sym in eng.universe:
            st = eng.data.states.get(sym)
            if st is None or st.last_ts is None:
                continue
            vwap = st.vwap
            ctxs[sym] = TwinCtx(
                st.last_ts, st.last_close,
                eligible=st.bar_count >= p.min_bars_before_entry and st.dev_pct is not None and st.vol_ratio is not None,
                exit=lambda pos, c=st.last_close, v=vwap: check_exit(pos, c, v, p),
                size=lambda side, price: max(1, int(p.notional_per_trade // price)) if price > 0 else 0)
        self.book.step(now, ctxs, entries_ok)

    def eod(self, eng, now: datetime) -> None:
        prices = {s: eng.last_price(s) for s in list(self.book.positions)}
        self.book.flatten(now, prices)

    # ---- S1–S3 ---------------------------------------------------------------
    def on_cycle_bars(self, sleeve, now: datetime, ctxs: dict[str, TwinCtx], entries_ok: bool) -> None:
        self.ensure_day(now)
        self.book.step(now, ctxs, entries_ok)


def replay_twin_s0(day, params, seed: int, p: float) -> TwinBook:
    """S0's twin for one stored day (a `replay.DayData`): the same TwinBook, fed minute by minute the way the
    live cycle feeds it, so `seed` + `p` + the day's bars reproduce the live twin's trades."""
    from datetime import timedelta
    book = TwinBook("S0", allow_short=True)
    book.set_day(day.date.isoformat(), seed, p)
    session = day.session
    last: dict = {}
    for m in day.minutes:
        now = m + timedelta(minutes=1, seconds=5)
        if now >= session.flatten_at:
            break
        for snap in day.at[m]:
            last[snap.symbol] = snap
        ctxs = {}
        for sym, snap in last.items():
            ctxs[sym] = TwinCtx(
                snap.ts, snap.last_close,
                eligible=snap.bar_count >= params.min_bars_before_entry and snap.dev_pct is not None
                and snap.vol_ratio is not None,
                exit=lambda pos, c=snap.last_close, v=snap.vwap: check_exit(pos, c, v, params),
                size=lambda side, price: max(1, int(params.notional_per_trade // price)) if price > 0 else 0)
        book.step(now, ctxs, session.entries_allowed(now))
    book.flatten(session.flatten_at, {s: sn.last_close for s, sn in day.last_snapshot.items()})
    return book


def edge(store, sleeve_id: str, day: str, trailing: int = TRAILING_SESSIONS) -> dict:
    """The scoreboard metric (§4): sleeve net P&L − twin net P&L, for `day` and its trailing sessions."""
    dates = [d for d in store.cycle_dates(trailing + 5, sleeve_id) if d <= day]
    if day not in dates:
        dates.append(day)
    dates = sorted(dates)[-trailing:]
    s_day, t_day = store.sleeve_net_between(sleeve_id, day, day), store.twin_net_between(sleeve_id, day, day)
    s_tr = store.sleeve_net_between(sleeve_id, dates[0], day)
    t_tr = store.twin_net_between(sleeve_id, dates[0], day)
    return {"sleeve_id": sleeve_id, "day": day, "sleeve_net": round(s_day, 2), "twin_net": round(t_day, 2),
            "edge": round(s_day - t_day, 2), "sessions": len(dates), "sleeve_net_trailing": round(s_tr, 2),
            "twin_net_trailing": round(t_tr, 2), "edge_trailing": round(s_tr - t_tr, 2)}
