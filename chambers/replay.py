"""Deterministic replay over stored bars.

Uses the exact `strategy.evaluate` and the exact exit logic from `engine.py`
(`Position`, `check_exit`, `trade_economics`). The mock broker fills at the
bar close and quotes a fixed 0.02% spread (no live quotes in replay).

Bars are first reduced to per-bar `Snapshot`s (close, vwap, vol_ratio, ...),
which do not depend on strategy parameters, so the nightly sweep builds them
once per day and replays every parameter combination over the same snapshots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .clock import ET, Session
from .data import SymbolState
from .engine import Position, check_exit, trade_economics
from .store import Bar, Store
from .strategy import Params, evaluate, hypothesis, position_qty

REPLAY_SPREAD = 0.0002   # 0.02% of price, bid/ask straddle the close


class Snapshot:
    """What `strategy.evaluate` needs from a SymbolState, frozen at one bar."""
    __slots__ = ("symbol", "ts", "last_close", "vwap", "dev_pct", "vol_ratio", "bar_count")

    def __init__(self, symbol: str, ts: datetime, last_close: float, vwap: Optional[float],
                 dev_pct: Optional[float], vol_ratio: Optional[float], bar_count: int):
        self.symbol = symbol
        self.ts = ts
        self.last_close = last_close
        self.vwap = vwap
        self.dev_pct = dev_pct
        self.vol_ratio = vol_ratio
        self.bar_count = bar_count


class ReplayBroker:
    """Mock broker: fills at the bar close, quotes a fixed spread."""

    def __init__(self, spread: float = REPLAY_SPREAD):
        self.spread = spread

    def quote(self, close: float) -> tuple[float, float]:
        half = close * self.spread / 2.0
        return close - half, close + half

    def fill(self, close: float) -> float:
        return close


@dataclass
class DayData:
    date: date
    session: Session
    minutes: list[datetime]                      # sorted bar timestamps that exist for any symbol
    at: dict[datetime, list[Snapshot]]           # bar ts → snapshots of symbols that have that bar
    last_snapshot: dict[str, Snapshot]           # symbol → last snapshot of the day (for eod fill)
    bar_count: int = 0


def default_session(d: date) -> Session:
    """Regular 9:30–16:00 session, used only when no calendar is available for a replay date."""
    return Session(d, datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                   datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))


def build_day(d: date, bars_by_symbol: dict[str, list[Bar]], session: Optional[Session] = None) -> DayData:
    session = session or default_session(d)
    at: dict[datetime, list[Snapshot]] = {}
    last: dict[str, Snapshot] = {}
    n = 0
    for sym, bars in bars_by_symbol.items():
        st = SymbolState(sym)
        for b in sorted(bars, key=lambda x: x.ts):
            if not (session.open <= b.ts < session.close):
                continue
            st.update([b])
            snap = Snapshot(sym, b.ts, st.last_close, st.vwap, st.dev_pct, st.vol_ratio, st.bar_count)
            at.setdefault(b.ts, []).append(snap)
            last[sym] = snap
            n += 1
    return DayData(d, session, sorted(at), at, last, n)


@dataclass
class ReplayTrade:
    symbol: str
    side: str
    qty: int
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
    hypothesis: dict

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["entry_ts"] = self.entry_ts.isoformat(timespec="seconds")
        d["exit_ts"] = self.exit_ts.isoformat(timespec="seconds")
        return d


@dataclass
class ReplayResult:
    date: date
    params: Params
    trades: list[ReplayTrade] = field(default_factory=list)
    signals_evaluated: int = 0
    signals_fired: int = 0

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def est_cost(self) -> float:
        return sum(t.est_cost for t in self.trades)

    def exit_reasons(self) -> dict:
        out: dict = {}
        for t in self.trades:
            out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
        return out


def replay(day: DayData, params: Params, broker: Optional[ReplayBroker] = None,
           collect_hypothesis: bool = True) -> ReplayResult:
    """Replay one day. Mirrors Engine.run_cycle: for the cycle that sees bar `m` (at m+1min+5s):
    exits on open positions first, then entries for every symbol; eod flatten at flatten_at."""
    broker = broker or ReplayBroker()
    session = day.session
    res = ReplayResult(day.date, params)
    positions: dict[str, Position] = {}
    cycle_offset = timedelta(minutes=1, seconds=5)

    def close(pos: Position, snap_close: float, ts: datetime, reason: str) -> None:
        exit_px = broker.fill(snap_close)
        bid, ask = broker.quote(snap_close)
        pos.update_excursion(exit_px)
        gross, cost, net = trade_economics(pos.side, pos.qty, pos.entry_price, exit_px,
                                           pos.entry_bid, pos.entry_ask, bid, ask)
        res.trades.append(ReplayTrade(pos.symbol, pos.side, pos.qty, pos.entry_ts, pos.entry_price, ts, exit_px,
                                      reason, pos.bars_held, pos.mae_pct, pos.mfe_pct, gross, cost, net,
                                      pos.__dict__.pop("_hyp", {}) if collect_hypothesis else {}))
        del positions[pos.symbol]

    for m in day.minutes:
        cycle_now = m + cycle_offset
        if cycle_now >= session.flatten_at:
            break
        snaps = day.at[m]
        # exits first
        for snap in snaps:
            pos = positions.get(snap.symbol)
            if pos is None:
                continue
            pos.on_bar(snap.ts, snap.last_close)
            reason = check_exit(pos, snap.last_close, snap.vwap, params)
            if reason:
                close(pos, snap.last_close, cycle_now, reason)
        # entries
        entries_ok = session.entries_allowed(cycle_now)
        if not entries_ok:
            continue
        for snap in snaps:
            sym = snap.symbol
            slots = len(positions) < params.max_open_positions
            sig = evaluate(snap, params, sym in positions, slots, True)
            res.signals_evaluated += 1
            if not sig.fired:
                continue
            res.signals_fired += 1
            px = broker.fill(snap.last_close)
            bid, ask = broker.quote(snap.last_close)
            qty = position_qty(px, params)
            pos = Position(None, sym, sig.side, qty, px, cycle_now, snap.ts, entry_bid=bid, entry_ask=ask)
            if collect_hypothesis:
                pos.__dict__["_hyp"] = hypothesis(sig, params)
            positions[sym] = pos

    # eod flatten: everything still open closes at its last close of the day
    for sym in list(positions):
        pos = positions[sym]
        snap = day.last_snapshot.get(sym)
        px = snap.last_close if snap else pos.entry_price
        close(pos, px, session.flatten_at, "eod_flatten")
    return res


def replay_day(store: Store, d: date, params: Params, session: Optional[Session] = None) -> ReplayResult:
    bars = store.bars_for_day(d)
    return replay(build_day(d, bars, session), params)


def format_replay(res: ReplayResult) -> str:
    lines = [f"replay {res.date}  params={res.params.to_dict()}", ""]
    lines.append(f"{'entry':<8} {'exit':<8} {'sym':<6} {'side':<5} {'qty':>5} {'in':>10} {'out':>10} "
                 f"{'bars':>4} {'mae%':>7} {'mfe%':>7} {'gross':>9} {'cost':>7} {'net':>9}  reason")
    for t in res.trades:
        lines.append(f"{t.entry_ts.strftime('%H:%M:%S'):<8} {t.exit_ts.strftime('%H:%M:%S'):<8} {t.symbol:<6} "
                     f"{t.side:<5} {t.qty:>5} {t.entry_price:>10.4f} {t.exit_price:>10.4f} {t.bars_held:>4} "
                     f"{t.mae_pct:>7.3f} {t.mfe_pct:>7.3f} {t.gross_pnl:>9.2f} {t.est_cost:>7.2f} "
                     f"{t.net_pnl:>9.2f}  {t.exit_reason}")
    lines.append("")
    lines.append(f"trades={len(res.trades)} evaluated={res.signals_evaluated} fired={res.signals_fired} "
                 f"gross={res.gross_pnl:.2f} cost={res.est_cost:.2f} net={res.net_pnl:.2f} "
                 f"exits={res.exit_reasons()}")
    return "\n".join(lines)
