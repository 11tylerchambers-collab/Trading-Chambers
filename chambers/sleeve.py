"""Live runner for the bar sleeves S1–S3 (PHASE1A §2, §10).

`BarSleeve` shares order execution, partial-fill handling, flatten, reconcile and the entry gates with
S0 through `engine.SleeveBase`, and the strategy/exit code with replay through `strategies.py`.
What differs per sleeve is where its bars come from and when it runs:

  S1  15-minute bars built from 1-minute SPY/QQQ bars; cycles 5 s after each 15-minute bucket ends,
      entries in the Phase 0 window (open+5 min … close−15 min), EOD flatten at flatten_at.
  S2  Alpaca 1-hour BTC/USD bars; cycles at hh:00:10 UTC, 24/7; daily sweep at 00:30 UTC.
  S3  session-4h GLD/USO bars; decides at 13:30:05 and at flatten_at (last bar, see bars.py); no EOD
      flatten — positions are held overnight and adopted by reconcile after a restart.

Sweeps: S1 and S3 at sweep_at (after S0's), S2 at its 00:30 UTC rollover. One sweep per sleeve per day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from . import history
from .bars import TF_1H, TF_S4H, build_15m, build_session_4h, s1_decision_times, s4h_decision_times
from .clock import ET, MarketClock, Session
from .engine import CycleResult, Position, SleeveBase
from .replay import default_session
from .risk import risk_qty
from .store import Bar, Store, Trade, iso, parse_ts
from .strategies import SPECS, StrategySpec, evaluate_entry, hypothesis, stop_price_for

log = logging.getLogger("chambers.sleeve")

UTC = timezone.utc
S1_PREV_SESSIONS = 3            # previous sessions of 15-min bars S1 keeps for its indicators
S1_HISTORY_SESSIONS = 20        # sweep window (fetched on first run)
S2_HISTORY_DAYS = 35
S3_HISTORY_SESSIONS = 120
DEFAULT_CAPITAL = 20000.0


# ==========================================================================
# data sources
# ==========================================================================

class S1Source:
    """15-minute SPY/QQQ bars: previous sessions from the store, today's rebuilt from 1-minute bars."""

    def __init__(self, sleeve: "BarSleeve"):
        self.s = sleeve
        self._prev: dict[str, list[Bar]] = {}
        self._prev_day: Optional[date] = None

    def ensure_history(self, now: datetime) -> None:
        history.ensure_1m(self.s.store, self.s.broker, self.s.clock, self.s.symbols, S1_HISTORY_SESSIONS,
                          now.date())

    def _load_prev(self, today: date) -> None:
        if self._prev_day == today:
            return
        st = self.s.store
        dates = st.bar_dates(S1_PREV_SESSIONS, symbols=self.s.symbols, before=today)
        sessions = self.s.sessions_for(dates)
        self._prev = {s: [] for s in self.s.symbols}
        for ds in dates:
            d = date.fromisoformat(ds)
            raw = st.bars_for_day(d, symbols=self.s.symbols)
            for s in self.s.symbols:
                self._prev[s] += build_15m(raw.get(s, []), sessions.get(d) or default_session(d))
        self._prev_day = today

    def hist(self, now: datetime, mark: datetime, session: Optional[Session]) -> dict[str, list[Bar]]:
        if session is None:
            return {}
        self._load_prev(session.date)
        st, syms = self.s.store, self.s.symbols
        cutoff = min(mark.replace(second=0, microsecond=0), session.close)
        last = st.latest_bar_ts(session.date, syms)
        start = parse_ts(last) + timedelta(minutes=1) if last else session.open
        end = cutoff - timedelta(seconds=1)
        if start <= end:
            for sym, bars in self.s.broker.bars_1m(syms, start, end).items():
                st.write_bars(sym, [b for b in bars if session.open <= b.ts < session.close])
        today = st.bars_for_day(session.date, symbols=syms)
        return {s: (self._prev.get(s, []) + build_15m(today.get(s, []), session, cutoff))[-self.s.spec.hist_bars:]
                for s in syms}

    def last_1m_close(self, session: Session) -> dict[str, float]:
        out = {}
        for s, bars in self.s.store.bars_for_day(session.date, symbols=self.s.symbols).items():
            before = [b for b in bars if b.ts < session.flatten_at]
            if before:
                out[s] = before[-1].c
        return out


class S2Source:
    """1-hour BTC/USD bars from Alpaca, stored in bars_tf('1h'); only complete hours."""

    def __init__(self, sleeve: "BarSleeve"):
        self.s = sleeve

    def ensure_history(self, now: datetime) -> None:
        history.ensure_crypto_1h(self.s.store, self.s.broker, self.s.symbols, S2_HISTORY_DAYS, now)

    def hist(self, now: datetime, mark: datetime, session: Optional[Session]) -> dict[str, list[Bar]]:
        cutoff = mark.replace(second=0, microsecond=0)
        history.ensure_crypto_1h(self.s.store, self.s.broker, self.s.symbols, S2_HISTORY_DAYS, cutoff)
        return {s: self.s.store.bars_tf(s, TF_1H, None, cutoff, limit=self.s.spec.hist_bars)
                for s in self.s.symbols}


class S3Source:
    """Session-4h GLD/USO bars: stored full bars for earlier sessions, today's built from 1-minute bars."""

    def __init__(self, sleeve: "BarSleeve"):
        self.s = sleeve

    def ensure_history(self, now: datetime) -> None:
        history.ensure_s4h(self.s.store, self.s.broker, self.s.clock, self.s.symbols, S3_HISTORY_SESSIONS,
                           now.date())

    def _today_1m(self, session: Session, end: datetime) -> dict[str, list[Bar]]:
        st, syms = self.s.store, self.s.symbols
        last = st.latest_bar_ts(session.date, syms)
        start = parse_ts(last) + timedelta(minutes=1) if last else session.open
        if start <= end:
            for sym, bars in self.s.broker.bars_1m(syms, start, end).items():
                st.write_bars(sym, [b for b in bars if session.open <= b.ts < session.close])
        return st.bars_for_day(session.date, symbols=syms)

    def hist(self, now: datetime, mark: datetime, session: Optional[Session]) -> dict[str, list[Bar]]:
        if session is None:     # weekend / holiday restart: the stored bars are the whole history
            return {s: self.s.store.bars_tf(s, TF_S4H, None, now, limit=self.s.spec.hist_bars) for s in self.s.symbols}
        cutoff = min(mark.replace(second=0, microsecond=0), session.close)
        today = self._today_1m(session, cutoff - timedelta(seconds=1))
        day_start = session.open
        out = {}
        for s in self.s.symbols:
            prev = self.s.store.bars_tf(s, TF_S4H, None, day_start, limit=self.s.spec.hist_bars)
            now_bars = build_session_4h([b for b in today.get(s, []) if b.ts < cutoff], session, cutoff,
                                        partial_last=cutoff >= session.flatten_at)
            out[s] = (prev + now_bars)[-self.s.spec.hist_bars:]
        return out

    def finalize(self, session: Session) -> int:
        """After the close: store the day's full session-4h bars (replacing nothing earlier)."""
        today = self._today_1m(session, session.close - timedelta(seconds=1))
        n = 0
        for s in self.s.symbols:
            n += self.s.store.write_bars_tf(s, TF_S4H, build_session_4h(today.get(s, []), session), replace=True)
        return n


SOURCES = {"S1": S1Source, "S2": S2Source, "S3": S3Source}


# ==========================================================================
# the sleeve
# ==========================================================================

class BarSleeve(SleeveBase):
    def __init__(self, spec: StrategySpec, store: Store, broker, clock: MarketClock, config: Optional[dict] = None,
                 sleep_fn: Callable[[float], None] = time.sleep, capital: float = DEFAULT_CAPITAL,
                 fee_rate: float = 0.0, source=None, fetch_history: bool = True):
        self._init_base(store, broker, clock, sleep_fn)
        self.spec = spec
        self.sleeve_id = spec.sleeve_id
        self.symbols = list(spec.symbols)
        self.eod_flatten = spec.eod_flatten
        self.equity_sleeve = not spec.fractional
        self.fractional = spec.fractional
        self.fee_rate = fee_rate if spec.fractional else 0.0
        self.capital = capital
        self.config_params = spec.params(config or {})
        self.params = dict(self.config_params)
        self.source = source or SOURCES[spec.sleeve_id](self)
        self.fetch_history = fetch_history
        self.hist: dict[str, list[Bar]] = {}
        self.exit_bar_ts: dict[str, datetime] = {}
        self._cur_bar: dict[str, datetime] = {}
        self._last_mark: Optional[datetime] = None
        self._preopen_done: Optional[date] = None
        self._flattened: Optional[date] = None
        self._finalized: Optional[date] = None
        self._swept: Optional[str] = None
        self._counter_day: Optional[date] = None
        self._sessions_cache: dict[date, Session] = {}
        self.after_rollover: Optional[Callable[[datetime], None]] = None   # S2: crypto recon (set by main)

    # ---- params ------------------------------------------------------------
    def load_params(self) -> dict:
        row = self.store.read_params(self.sleeve_id)
        if row is None:
            self.store.write_params(self.config_params, "config", self.now(), self.sleeve_id)
            self.store.write_params_history(self.now().date(), self.config_params, "config", None, self.sleeve_id)
            self.params = dict(self.config_params)
        else:
            try:
                p = self.spec.params(row["params"])
                self.spec.validate(p)
                self.params = p
            except (ValueError, TypeError) as e:
                self._log_error("params", e)
        return self.params

    def sleeve_capital(self) -> float:
        eq = self.store.sleeve_equity(self.sleeve_id)
        return eq if eq is not None else self.capital

    def sessions_for(self, dates: list[str]) -> dict[date, Session]:
        want = [date.fromisoformat(d) for d in dates]
        if want and any(d not in self._sessions_cache for d in want):
            try:
                self._sessions_cache.update(self.clock.sessions_between(min(want) - timedelta(days=1), max(want)))
            except Exception as e:
                log.warning("%s calendar unavailable (%s); using 9:30-16:00", self.sleeve_id, e)
        return {d: self._sessions_cache[d] for d in want if d in self._sessions_cache}

    # ---- hooks -------------------------------------------------------------
    def last_price(self, symbol: str) -> Optional[float]:
        h = self.hist.get(symbol)
        return h[-1].c if h else None

    def quotes_for(self, symbols: list[str]) -> dict:
        return self.broker.quotes(self.symbols)

    def on_exit_recorded(self, pos: Position) -> None:
        bar_ts = self._cur_bar.get(pos.symbol) or pos.last_bar_ts
        if bar_ts is not None:
            self.exit_bar_ts[pos.symbol] = bar_ts
            if pos.trade_id is not None:
                self.store.set_exit_bar(pos.trade_id, bar_ts)

    def exit_qty(self, pos: Position) -> float:
        """Crypto: never sell more than the broker holds (Alpaca takes the buy fee out of the coins)."""
        if not self.fractional:
            return pos.qty
        try:
            held = sum(p["qty"] for p in self.broker.positions() if p["symbol"] == pos.symbol)
        except Exception:
            return pos.qty
        if 0 < held < pos.qty:
            return float(int(held * 1e8) / 1e8)
        return pos.qty

    def adopt(self, t: Trade) -> Position:
        hyp = t.hypothesis
        entry_bar = parse_ts(hyp["entry_bar_ts"]) if hyp.get("entry_bar_ts") else None
        pos = Position(t.id, t.symbol, t.side, t.qty, t.entry_price, parse_ts(t.entry_ts), entry_bar,
                       bars_held=0, mae_pct=t.mae_pct, mfe_pct=t.mfe_pct, entry_bid=t.entry_bid,
                       entry_ask=t.entry_ask, stop_price=hyp.get("stop_price"))
        for b in self.hist.get(t.symbol, []):
            if entry_bar is None or b.ts > entry_bar:
                pos.on_bar(b.ts, b.c)
        if entry_bar is None:
            pos.bars_held = t.bars_held
        pos.mae_pct = min(pos.mae_pct, t.mae_pct)
        pos.mfe_pct = max(pos.mfe_pct, t.mfe_pct)
        return pos

    def bars_since_exit(self, symbol: str, h: list[Bar]) -> Optional[int]:
        ts = self.exit_bar_ts.get(symbol)
        if ts is None:
            return None
        return sum(1 for b in h if b.ts > ts)

    def entries_allowed(self, now: datetime, session: Optional[Session]) -> bool:
        if self.spec.sleeve_id == "S2":
            return True
        if session is None:
            return False
        if self.spec.sleeve_id == "S3":
            return session.in_session(now)
        return session.entries_allowed(now)

    # ---- lifecycle ---------------------------------------------------------
    def startup(self, reconcile: bool = True) -> None:
        now = self.now()
        self.store.write_heartbeat(self.sleeve_id, state="idle", pid=os.getpid(), started_at=now)
        self.load_params()
        self._init_counters_for(now.date())
        self._counter_day = now.date()
        self.exit_bar_ts = {s: parse_ts(ts) for s, ts in self.store.last_exit_bars(self.sleeve_id).items()}
        last = self.store.last_cycle(self.sleeve_id)
        if last is not None:
            # a decision mark is never run twice: marks at or before the last cycle count as done
            self._last_mark = parse_ts(last["ts"])
        if self.fetch_history:
            try:
                self.source.ensure_history(now)
            except Exception as e:
                self._log_error("startup.history", e)
        try:
            self.hist = self.source.hist(now, now, self.clock.today_session())
        except Exception as e:
            self._log_error("startup.bars", e)
        if self.twin is not None:
            self.twin.restore(now)
        if reconcile:
            self.reconcile()
        self._write_heartbeat()

    def preopen(self, session: Session) -> None:
        self.set_state("preopen")
        self.load_params()
        self._init_counters_for(session.date)
        self._counter_day = session.date
        if self.fetch_history:
            try:
                self.source.ensure_history(self.now())
            except Exception as e:
                self._log_error("preopen.history", e)
        self.reconcile()
        self._preopen_done = session.date
        self._write_heartbeat()

    # ---- the cycle ---------------------------------------------------------
    def run_cycle(self, mark: Optional[datetime] = None) -> CycleResult:
        t0 = time.monotonic()
        now = self.now()
        mark = mark or now
        self._last_mark = mark
        res = CycleResult(ts=now, state=self.state, sleeve_id=self.sleeve_id)
        session = self.clock.today_session()
        if self._counter_day != now.date():
            self._init_counters_for(now.date())
            self._counter_day = now.date()
        paused = False
        try:
            self.load_params()
            paused = self.store.read_controls()["paused"]
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.controls", e)
        self.risk_tick(now, res)
        p = self.params

        bars_ok = False
        try:
            self.hist = self.source.hist(now, mark, session)
            bars_ok = True
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.bars", e)

        quotes: dict = {}
        try:
            quotes = self.broker.quotes(self.symbols) or {}
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.quotes", e)

        rows: list[dict] = []
        entries_ok = self.entries_allowed(now, session) and not paused
        if bars_ok:
            self._cur_bar = {s: h[-1].ts for s, h in self.hist.items() if h}
            # exits
            for pos in list(self.positions.values()):
                try:
                    h = self.hist.get(pos.symbol) or []
                    if not h:
                        continue
                    for b in h:
                        if pos.last_bar_ts is None or b.ts > pos.last_bar_ts:
                            pos.on_bar(b.ts, b.c)
                    if pos.trade_id is not None:
                        self.store.update_trade_progress(pos.trade_id, pos.bars_held, pos.mae_pct, pos.mfe_pct)
                    reason = self.spec.exit(pos, h, p)
                    if reason:
                        self.close_position(pos, reason, quotes, now)
                        res.orders_placed += 1
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.exit", e)
            # entries — every symbol evaluated and logged every cycle
            for sym in self.symbols:
                try:
                    h = self.hist.get(sym) or []
                    ev = evaluate_entry(self.spec, h, p, sym in self.positions, self.bars_since_exit(sym, h),
                                        entries_ok)
                    reason, fired, qty = ev.reason, ev.fired, 0
                    if fired and self.spec.pair_filter and any(
                            q.side == ev.side for s, q in self.positions.items() if s != sym):
                        reason, fired = "pair_filter", False
                    if fired:
                        qty, why = risk_qty(self.sleeve_capital(), p["risk_pct"], ev.stop_distance, ev.price,
                                            p["max_notional_pct"], self.fractional)
                        if why:
                            reason, fired = why, False
                        else:
                            gate = self.entry_gate(sym, ev.side, qty * ev.price, p["skip_news_days"], now)
                            if gate:
                                reason, fired = gate, False
                    res.symbols_evaluated += 1
                    res.reasons[reason] = res.reasons.get(reason, 0) + 1
                    rows.append({"ts": now, "symbol": sym, "close": ev.price, "side": ev.side if fired else None,
                                 "fired": fired, "reason": reason, "params": p,
                                 "vol_ratio": ev.detail.get("vol_ratio"),
                                 "detail": {**ev.detail, "stop_distance": ev.stop_distance, "qty": qty or None,
                                            "bar_ts": iso(h[-1].ts) if h else None}})
                    if fired:
                        res.signals_fired += 1
                        self.open_position(sym, ev, qty, h, quotes, now)
                        res.orders_placed += 1
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.entry", e)
            # twin
            if self.twin is not None:
                try:
                    from .sleeve_replay import bar_twin_ctx
                    cap = self.sleeve_capital()
                    ctxs = {s: bar_twin_ctx(self.spec, h, p, cap) for s, h in self.hist.items() if h}
                    self.twin.on_cycle_bars(self, now, ctxs, self.entries_allowed(now, session))
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.twin", e)

        res.duration_ms = int((time.monotonic() - t0) * 1000)
        try:
            res.cycle_id = self.store.write_cycle(now, self.state, res.symbols_evaluated, res.signals_fired,
                                                  res.orders_placed, res.errors, res.duration_ms, self.sleeve_id)
            self.store.write_signals(res.cycle_id, rows, self.sleeve_id)
            self.counters["cycles_today"] += 1
            self.counters["signals_today"] += len(rows)
            self.counters["fired_today"] += res.signals_fired
            self._write_heartbeat(last_cycle_ts=now)
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.write", e)
        log.info("%s", res)
        return res

    def open_position(self, sym: str, ev, qty: float, h: list[Bar], quotes: dict, now: datetime) -> Optional[Position]:
        q = quotes.get(sym) or {}
        got = self.enter(sym, ev.side, qty, ev.price, now)
        if got is None:
            return None
        qty, px, oid = got
        stop = stop_price_for(ev.side, px, ev.stop_distance)
        hyp = hypothesis(self.spec, ev, self.params, stop, iso(h[-1].ts))
        news_day, event = self.news(now.date())
        tid = self.store.open_trade(sym, ev.side, qty, now, px, oid, q.get("bid"), q.get("ask"), hyp, self.params,
                                    self.sleeve_id, news_day=news_day, event=event)
        pos = Position(tid, sym, ev.side, qty, px, now, h[-1].ts, entry_bid=q.get("bid"), entry_ask=q.get("ask"),
                       stop_price=stop)
        self.positions[sym] = pos
        self.counters["opened_today"] += 1
        log.info("OPEN %s %s %s x%s @ %.4f stop %.4f", self.sleeve_id, ev.side, sym, qty, px, stop)
        return pos

    # ---- sweep ---------------------------------------------------------------
    def run_sweep(self, key: date) -> Optional[dict]:
        from .sleeve_replay import run_bar_sweep
        try:
            sessions = {}
            if self.spec.sleeve_id != "S2":
                try:
                    sessions = self.clock.sessions_between(key - timedelta(days=200), key)
                except Exception as e:
                    log.warning("calendar unavailable for the sweep: %s", e)
            return run_bar_sweep(self.store, self.spec, self.params, key, self.now(), self.sleeve_capital(),
                                 self.fee_rate, sessions)
        except Exception as e:
            self._log_error("sweep", e)
            return None

    def fallback_rate(self) -> Optional[float]:
        """Twin p with no live history: the replay firing rate at current params over the sweep window."""
        from .sleeve_replay import load_window, replay_bars
        series, events, _ = load_window(self.spec, self.store, self.now().date())
        if not events:
            return None
        r = replay_bars(self.spec, series, events, self.params, self.sleeve_capital(), self.fee_rate)
        return r.fired / r.evaluated if r.evaluated else None

    # ---- schedule ------------------------------------------------------------
    def decision_times(self, session: Session) -> list[datetime]:
        if self.spec.sleeve_id == "S1":
            return s1_decision_times(session)
        return s4h_decision_times(session)

    def plan(self, now: datetime):
        if self.spec.sleeve_id == "S2":
            return self._plan_24x7(now)
        session = self.clock.today_session()
        if session is None:
            self.set_state("idle")
            nxt = self.clock.next_session()
            return min(nxt.preopen_at if nxt else now + timedelta(hours=1), now + timedelta(minutes=10)), None
        if now < session.preopen_at:
            self.set_state("idle")
            return session.preopen_at, None
        if self._preopen_done != session.date and now < session.close:
            self.preopen(session)
        marks = self.decision_times(session)
        due = [m for m in marks if m <= now and (self._last_mark is None or m > self._last_mark)]
        if due:
            m = due[-1]
            later = [x for x in marks if x > m]
            limit = min(later[0] if later else session.close, session.close)
            if now < limit:
                self.set_state("running")
                return now, (lambda m=m: self.run_cycle(m))
            self._last_mark = m    # missed (process down): never trade on a stale mark
        if self.eod_flatten and now >= session.flatten_at and self._flattened != session.date:
            self.set_state("flattening")
            self.flatten("eod_flatten", now)
            if self.twin is not None:
                self.twin.book.flatten(now, self.source.last_1m_close(session) if hasattr(self.source, "last_1m_close")
                                       else {s: self.last_price(s) for s in self.twin.book.positions})
            self._flattened = session.date
            self.set_state("postclose")
            return now, None
        nxt = [m for m in marks if m > now]
        if nxt:
            self.set_state("running" if now >= session.open else "preopen")
            return nxt[0], (lambda m=nxt[0]: self.run_cycle(m))
        if self.eod_flatten and now < session.flatten_at:
            return session.flatten_at, None
        if now < session.sweep_at:
            self.set_state("postclose")
            return session.sweep_at, None
        if self._finalized != session.date and hasattr(self.source, "finalize"):
            try:
                self.source.finalize(session)
            except Exception as e:
                self._log_error("postclose.finalize", e)
            self._finalized = session.date
        if self._swept != session.date.isoformat():
            if not self.store.has_sweep_for(session.date, self.sleeve_id):
                self.set_state("sweeping")
                self.run_sweep(session.date)
            self._swept = session.date.isoformat()
            self.set_state("idle")
            return now, None
        self.set_state("idle")
        nxt_s = self.clock.next_session()
        return min(nxt_s.preopen_at if nxt_s else now + timedelta(hours=1), now + timedelta(minutes=10)), None

    def _plan_24x7(self, now: datetime):
        u = now.astimezone(UTC)
        mark = u.replace(minute=0, second=10, microsecond=0)
        if mark > u:
            mark -= timedelta(hours=1)
        if (self._last_mark is None or mark > self._last_mark) and (u - mark) < timedelta(minutes=50):
            self.set_state("running")
            return now, (lambda m=mark: self.run_cycle(m))
        roll = u.replace(hour=0, minute=30, second=0, microsecond=0)
        if roll > u:
            roll -= timedelta(days=1)
        key = (roll - timedelta(days=1)).date()            # the UTC day that just ended
        if self._swept != key.isoformat():
            if not self.store.has_sweep_for(key, self.sleeve_id):
                self.set_state("sweeping")
                self.run_sweep(key)
                if self.after_rollover is not None:
                    try:
                        self.after_rollover(now)
                    except Exception as e:
                        self._log_error("rollover", e)
            self._swept = key.isoformat()
            self.set_state("running")
            return now, None
        nxt_mark = mark + timedelta(hours=1)
        nxt_roll = roll + timedelta(days=1)
        if nxt_mark <= nxt_roll:
            return nxt_mark.astimezone(ET), (lambda m=nxt_mark: self.run_cycle(m))
        return nxt_roll.astimezone(ET), None


def make_sleeve(sleeve_id: str, store: Store, broker, clock: MarketClock, cfg: dict,
                sleep_fn: Callable[[float], None] = time.sleep, **kw) -> BarSleeve:
    """Build S1/S2/S3 from config.yaml's `sleeves:` section."""
    spec = SPECS[sleeve_id]
    sc = (cfg.get("sleeves") or {}).get(sleeve_id) or {}
    return BarSleeve(spec, store, broker, clock, sc.get("params") or {}, sleep_fn,
                     capital=float(sc.get("capital", DEFAULT_CAPITAL)),
                     fee_rate=float((cfg.get("crypto") or {}).get("fee_rate", 0.0025)), **kw)
