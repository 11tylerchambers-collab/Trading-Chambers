"""Replay and nightly sweep for the bar sleeves S1–S3 (PHASE1A §2, §2.4).

Replay walks a list of decision events — one per cycle the live sleeve would have run — and at each
one does what `sleeve.BarSleeve.run_cycle` does: exits first, then entries, with the same strategy
functions (`strategies.py`), the same cooldown, pair filter and 1% ATR sizing (at a fixed capital),
and, optionally, the same random twin (`twin.TwinBook`). Fills are at the bar close with replay costs
(0.02% spread + slippage, + the crypto fee for S2). Portfolio caps (same side, exposure, daily loss)
are not applied: they depend on the other sleeves and are not part of a sleeve's own result.

Sweep (§2.4): the Phase 0 one-step rule and 20-trade evidence rule, per sleeve, over a window:
  S1  the last 20 sessions of 15-minute bars (built from stored 1-minute bars)
  S2  the last 30 days of 1-hour bars
  S3  up to 120 sessions of session-4h bars ("only if ≥ 20 closed trades exist in its lookback window")
Evidence: the current params must produce ≥ 20 closed trades over the window (otherwise
`insufficient_evidence`, params unchanged). A combination is eligible if it produced ≥ 20 trades over
the window. Score = total net P&L over the window. Idempotent per sleeve per day.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .bars import TF_1H, TF_S4H, build_15m, buckets_s4h
from .clock import ET, Session
from .engine import Position, trade_economics
from .replay import ReplayBroker, default_session
from .risk import risk_qty
from .store import Bar, Store
from .strategies import S1, S2, S3, StrategySpec, evaluate_entry, hypothesis, stop_price_for
from .twin import TwinBook, TwinCtx, day_seed

log = logging.getLogger("chambers.sleeve_replay")

WINDOWS = {"S1": 20, "S2": 30, "S3": 120}      # sessions (S1, S3) or days (S2)
MIN_WINDOW_TRADES = 20
S2_FEE_DEFAULT = 0.0025


@dataclass
class Event:
    t: datetime                        # decision time (the live cycle's time)
    idx: dict[str, int]                # symbol -> index of the bar evaluated in series[symbol]
    entries_allowed: bool
    flatten_prices: Optional[dict] = None   # S1: EOD flatten right after this event, at these prices
    day: str = ""


@dataclass
class BarTrade:
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
    stop_distance: float
    hypothesis: dict = field(default_factory=dict)


@dataclass
class BarReplayResult:
    params: dict
    trades: list[BarTrade] = field(default_factory=list)
    evaluated: int = 0
    fired: int = 0
    twin: Optional[TwinBook] = None

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def twin_net(self) -> float:
        return self.twin.net_pnl if self.twin else 0.0


# --------------------------------------------------------------------------
# the replay loop
# --------------------------------------------------------------------------

def replay_bars(spec: StrategySpec, series: dict[str, list[Bar]], events: list[Event], params: dict,
                capital: float, fee_rate: float = 0.0, twin_seed_for=None, twin_p: float = 0.0,
                close_at_end: bool = True) -> BarReplayResult:
    """`twin_seed_for(day) -> seed` turns on the random twin with entry probability `twin_p`."""
    p = spec.params(params)
    broker = ReplayBroker()
    res = BarReplayResult(p)
    positions: dict[str, Position] = {}
    exit_idx: dict[str, int] = {}
    twin = TwinBook(spec.sleeve_id, spec.allow_short, fee_rate) if twin_seed_for else None
    res.twin = twin
    hb = spec.hist_bars

    def hist(sym: str, i: int) -> list[Bar]:
        return series[sym][max(0, i + 1 - hb):i + 1]

    def close(pos: Position, price: float, t: datetime, reason: str, i: Optional[int] = None) -> None:
        bid, ask = broker.quote(price)
        pos.update_excursion(price)
        gross, cost, net = trade_economics(pos.side, pos.qty, pos.entry_price, price, pos.entry_bid, pos.entry_ask,
                                           bid, ask, fee_rate)
        res.trades.append(BarTrade(pos.symbol, pos.side, pos.qty, pos.entry_ts, pos.entry_price, t, price, reason,
                                   pos.bars_held, pos.mae_pct, pos.mfe_pct, gross, cost, net,
                                   pos.extra.get("stop_distance", 0.0), pos.extra.get("hyp", {})))
        positions.pop(pos.symbol, None)
        if i is not None:
            exit_idx[pos.symbol] = i

    last_prices: dict[str, float] = {}
    for ev in events:
        if twin is not None and twin.day != ev.day:
            twin.set_day(ev.day, twin_seed_for(ev.day), twin_p)
        # exits
        for sym, i in ev.idx.items():
            pos = positions.get(sym)
            if pos is None:
                continue
            h = hist(sym, i)
            pos.on_bar(h[-1].ts, h[-1].c)
            reason = spec.exit(pos, h, p)
            if reason:
                close(pos, h[-1].c, ev.t, reason, i)
        # entries
        for sym, i in ev.idx.items():
            h = hist(sym, i)
            last_prices[sym] = h[-1].c
            bse = (i - exit_idx[sym]) if sym in exit_idx else None
            e = evaluate_entry(spec, h, p, sym in positions, bse, ev.entries_allowed)
            if e.reason != "entries_closed":
                res.evaluated += 1
            if not e.fired:
                continue
            if spec.pair_filter and any(q.side == e.side for s, q in positions.items() if s != sym):
                continue
            qty, why = risk_qty(capital, p["risk_pct"], e.stop_distance, e.price, p["max_notional_pct"],
                                spec.fractional)
            if why:
                continue
            res.fired += 1
            bid, ask = broker.quote(e.price)
            stop = stop_price_for(e.side, e.price, e.stop_distance)
            pos = Position(None, sym, e.side, qty, e.price, ev.t, h[-1].ts, entry_bid=bid, entry_ask=ask,
                           stop_price=stop, extra={"stop_distance": e.stop_distance,
                                                   "hyp": hypothesis(spec, e, p, stop, h[-1].ts.isoformat())})
            positions[sym] = pos
        # twin
        if twin is not None:
            ctxs = {}
            for sym, i in ev.idx.items():
                h = hist(sym, i)
                ctxs[sym] = bar_twin_ctx(spec, h, p, capital)
            twin.step(ev.t, ctxs, ev.entries_allowed)
        # EOD flatten (S1)
        if ev.flatten_prices is not None:
            for sym, pos in list(positions.items()):
                close(pos, ev.flatten_prices.get(sym) or last_prices.get(sym) or pos.entry_price,
                      ev.t, "eod_flatten")
            if twin is not None:
                twin.flatten(ev.t, {**last_prices, **ev.flatten_prices})
    if close_at_end:
        t_end = events[-1].t if events else None
        for sym, pos in list(positions.items()):
            close(pos, last_prices.get(sym, pos.entry_price), t_end, "replay_end")
        if twin is not None and t_end is not None:
            twin.flatten(t_end, last_prices, "replay_end")
    return res


def bar_twin_ctx(spec: StrategySpec, h: list[Bar], p: dict, capital: float) -> TwinCtx:
    """The twin's view of one symbol: the sleeve's exit rule, ATR stop and 1% sizing."""
    e = spec.entry(h, p)
    eligible = e.reason != "insufficient_bars" and bool(e.stop_distance)
    sd = e.stop_distance or 0.0

    def size(side: str, price: float) -> float:
        q, why = risk_qty(capital, p["risk_pct"], sd, price, p["max_notional_pct"], spec.fractional)
        return 0 if why else q

    return TwinCtx(h[-1].ts, h[-1].c, eligible, exit=lambda pos: spec.exit(pos, h, p), size=size,
                   stop=lambda side, price: stop_price_for(side, price, sd) if sd else None)


# --------------------------------------------------------------------------
# data windows → (series, events)
# --------------------------------------------------------------------------

def _session_for(sessions: Optional[dict], d: date) -> Session:
    return (sessions or {}).get(d) or default_session(d)


def s1_events(day_bars: dict[str, dict[date, list[Bar]]], day_1m_last: dict[date, dict[str, float]],
              sessions: Optional[dict]) -> tuple[dict[str, list[Bar]], list[Event]]:
    """day_bars: symbol -> date -> that session's 15-minute bars. Series are concatenated across sessions."""
    series: dict[str, list[Bar]] = {s: [] for s in day_bars}
    events: list[Event] = []
    dates = sorted({d for m in day_bars.values() for d in m})
    for d in dates:
        sess = _session_for(sessions, d)
        start_idx = {s: len(series[s]) for s in series}
        for s in series:
            series[s] += day_bars[s].get(d, [])
        by_t: dict[datetime, dict[str, int]] = {}
        for s in series:
            for k in range(start_idx[s], len(series[s])):
                b = series[s][k]
                t = min(b.ts + timedelta(minutes=15), sess.close) + timedelta(seconds=5)
                if t < sess.flatten_at:
                    by_t.setdefault(t, {})[s] = k
        day_events = [Event(t, idx, sess.entries_allowed(t), day=d.isoformat()) for t, idx in sorted(by_t.items())]
        if day_events:
            prices = day_1m_last.get(d) or {}
            day_events.append(Event(sess.flatten_at, {}, False, flatten_prices=prices, day=d.isoformat()))
        events += day_events
    return series, events


def s2_events(series: dict[str, list[Bar]]) -> list[Event]:
    by_t: dict[datetime, dict[str, int]] = {}
    for s, bars in series.items():
        for k, b in enumerate(bars):
            by_t.setdefault(b.ts + timedelta(hours=1, seconds=10), {})[s] = k
    return [Event(t, idx, True, day=t.astimezone(ET).date().isoformat()) for t, idx in sorted(by_t.items())]


def s3_events(series: dict[str, list[Bar]], sessions: Optional[dict]) -> list[Event]:
    by_t: dict[datetime, dict[str, int]] = {}
    for s, bars in series.items():
        for k, b in enumerate(bars):
            d = b.ts.date()
            sess = _session_for(sessions, d)
            buckets = buckets_s4h(sess)
            last_start = buckets[-1][0]
            if b.ts >= last_start:
                t = sess.flatten_at
            else:
                t = min(b.ts + timedelta(hours=4), sess.close) + timedelta(seconds=5)
            by_t.setdefault(t, {})[s] = k
    return [Event(t, idx, True, day=t.date().isoformat()) for t, idx in sorted(by_t.items())]


def load_window(spec: StrategySpec, store: Store, today: date, sessions: Optional[dict] = None,
                n: Optional[int] = None, until: Optional[datetime] = None) -> tuple[dict, list[Event], list[str]]:
    """(series, events, dates) for the sleeve's sweep window ending at (and including) `today`."""
    n = n or WINDOWS[spec.sleeve_id]
    syms = list(spec.symbols)
    if spec.sleeve_id == "S1":
        dates = store.bar_dates(n, symbols=syms, before=today + timedelta(days=1))
        day_bars: dict[str, dict[date, list[Bar]]] = {s: {} for s in syms}
        last_1m: dict[date, dict[str, float]] = {}
        for ds in dates:
            d = date.fromisoformat(ds)
            sess = _session_for(sessions, d)
            raw = store.bars_for_day(d, symbols=syms)
            last_1m[d] = {}
            for s in syms:
                day_bars[s][d] = build_15m(raw.get(s, []), sess)
                before_flat = [b for b in raw.get(s, []) if b.ts < sess.flatten_at]
                if before_flat:
                    last_1m[d][s] = before_flat[-1].c
        series, events = s1_events(day_bars, last_1m, sessions)
        return series, events, dates
    if spec.sleeve_id == "S2":
        end = until or datetime(today.year, today.month, today.day, tzinfo=ET) + timedelta(days=1)
        start = end - timedelta(days=n + 5)       # a few extra days so indicators are warm at the window start
        series = {s: store.bars_tf(s, TF_1H, start, end) for s in syms}
        events = [e for e in s2_events(series) if e.t >= end - timedelta(days=n)]
        return series, events, sorted({e.day for e in events})
    # S3
    end = datetime(today.year, today.month, today.day, tzinfo=ET) + timedelta(days=1)
    series = {s: store.bars_tf(s, TF_S4H, None, end) for s in syms}
    events = s3_events(series, sessions)
    dates = sorted({e.day for e in events})[-n:]
    events = [e for e in events if e.day in set(dates)]
    return series, events, dates


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def nearest_index(values: list, x) -> int:
    return min(range(len(values)), key=lambda i: (abs(values[i] - x), i))


def one_step(spec: StrategySpec, current: dict, best: dict) -> dict:
    """Each grid parameter moves at most one grid step toward `best`. If the result breaks the strategy's
    constraint (S3: fast < slow), the parameters are moved one at a time in grid order and any step that
    would break it is dropped."""
    out = dict(current)
    for key, values in spec.grid.items():
        ci = nearest_index(values, current[key])
        bi = nearest_index(values, best[key])
        ni = ci + (1 if bi > ci else -1 if bi < ci else 0)
        out[key] = values[ni]
    if spec.grid_ok(out):
        return out
    out = dict(current)
    for key, values in spec.grid.items():
        ci = nearest_index(values, current[key])
        bi = nearest_index(values, best[key])
        trial = dict(out)
        trial[key] = values[ci + (1 if bi > ci else -1 if bi < ci else 0)]
        if spec.grid_ok(trial):
            out = trial
    return out


def run_bar_sweep(store: Store, spec: StrategySpec, current: dict, today: date, now: datetime,
                  capital: float, fee_rate: float = 0.0, sessions: Optional[dict] = None,
                  profile: str = "LIVE", window=None) -> dict:
    t0 = time.monotonic()
    sid = spec.sleeve_id
    current = spec.params(current)
    if store.has_sweep_for(today, sid, profile):
        return {"date": today.isoformat(), "sleeve_id": sid, "reason": "already_swept: a sweep row for this date "
                "exists in params_history; params unchanged", "changed": False, "chosen": current, "results": []}
    series, events, dates = window or load_window(spec, store, today, sessions)
    summary: dict = {"date": today.isoformat(), "sleeve_id": sid, "days": dates, "current": current,
                     "combos_evaluated": 0, "eligible": 0, "best": None, "best_score": None, "chosen": current,
                     "changed": False, "reason": "", "results": [], "evidence_trades": 0}
    if not events:
        summary["reason"] = "no_data: no bars stored for the window yet"
    else:
        base = replay_bars(spec, series, events, current, capital, fee_rate)
        summary["evidence_trades"] = len(base.trades)
        results = []
        for combo in spec.grid_combos(current):
            r = replay_bars(spec, series, events, combo, capital, fee_rate)
            results.append({"params": {k: combo[k] for k in spec.grid}, "net_pnl": round(r.net_pnl, 2),
                            "trades": len(r.trades), "eligible": len(r.trades) >= MIN_WINDOW_TRADES})
        results.sort(key=lambda r: (-r["eligible"], -r["net_pnl"]))
        eligible = [r for r in results if r["eligible"]]
        summary.update(results=results, combos_evaluated=len(results), eligible=len(eligible))
        best = eligible[0] if eligible else None
        if best is not None:
            summary["best"], summary["best_score"] = best["params"], best["net_pnl"]
        if len(base.trades) < MIN_WINDOW_TRADES:
            summary["reason"] = (f"insufficient_evidence: {len(base.trades)} closed trades at the current params in "
                                 f"the {len(dates)}-{'day' if sid == 'S2' else 'session'} window (< {MIN_WINDOW_TRADES});"
                                 f" params unchanged")
        elif best is None:
            summary["reason"] = f"no_eligible_combination: none produced >= {MIN_WINDOW_TRADES} trades in the window"
        else:
            chosen = one_step(spec, current, {**current, **best["params"]})
            summary["chosen"] = chosen
            if chosen == current:
                summary["reason"] = "best_is_current: params unchanged"
            else:
                moved = {k: (current[k], chosen[k]) for k in spec.grid if current[k] != chosen[k]}
                capped = any(chosen[k] != best["params"][k] for k in spec.grid)
                summary["changed"] = True
                summary["reason"] = ("moved_one_step_toward_best" if capped else "moved_to_best") + ": " + \
                    ", ".join(f"{k} {a}->{b}" for k, (a, b) in moved.items())
    summary["duration_s"] = round(time.monotonic() - t0, 1)
    if summary["changed"]:
        store.write_params(summary["chosen"], "sweep", now, sid, profile)
    store.write_params_history(today, summary["chosen"], "sweep", summary, sid, profile)
    log.info("sweep %s %s: %s", sid, today, summary["reason"])
    return summary


def format_bar_replay(spec: StrategySpec, res: BarReplayResult, label: str) -> str:
    lines = [f"replay {spec.sleeve_id} {label}  params={res.params}", ""]
    lines.append(f"{'entry':<16} {'exit':<16} {'sym':<8} {'side':<5} {'qty':>10} {'in':>11} {'out':>11} {'bars':>4} "
                 f"{'gross':>9} {'cost':>7} {'net':>9}  reason")
    for t in res.trades:
        lines.append(f"{t.entry_ts.strftime('%m-%d %H:%M:%S'):<16} {t.exit_ts.strftime('%m-%d %H:%M:%S'):<16} "
                     f"{t.symbol:<8} {t.side:<5} {t.qty:>10.4f} {t.entry_price:>11.4f} {t.exit_price:>11.4f} "
                     f"{t.bars_held:>4} {t.gross_pnl:>9.2f} {t.est_cost:>7.2f} {t.net_pnl:>9.2f}  {t.exit_reason}")
    lines.append("")
    lines.append(f"trades={len(res.trades)} evaluated={res.evaluated} fired={res.fired} net={res.net_pnl:.2f}"
                 + (f" twin_net={res.twin_net:.2f} edge={res.net_pnl - res.twin_net:.2f}" if res.twin else ""))
    return "\n".join(lines)
