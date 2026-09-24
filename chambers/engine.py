"""Engine: cycle loop, positions, exits, flatten, reconcile, state machine.

The pure pieces at the top of this module (`Position`, `check_exit`,
`trade_economics`) are the single exit/accounting code path. `replay.py`
imports them so replay and live can never drift apart. Live trades are costed
with `live_trade_economics` instead, because their prices are real fills.

States: idle → preopen → running → flattening → postclose → sweeping → idle
"""
from __future__ import annotations

import logging
import os
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .broker import BrokerError
from .clock import ET, MarketClock, Session
from .data import DataState
from .store import Bar, Store, Trade
from .strategy import Params, Signal, bars_since, evaluate, hypothesis, position_qty

log = logging.getLogger("chambers.engine")

STATES = ("idle", "preopen", "running", "flattening", "postclose", "sweeping")
FILL_TIMEOUT_S = 5.0
FILL_SETTLE_S = 20.0         # extra wait for an order still working after FILL_TIMEOUT_S, before cancelling it
FILL_POLL_S = 0.25
TERMINAL_STATUSES = ("filled", "canceled", "cancelled", "rejected", "expired")
CYCLE_OFFSET_S = 5           # cycles run at hh:mm:05
SLIPPAGE_RATE = 0.0001       # 0.01% of notional, applied twice (entry + exit)


# ==========================================================================
# Pure, shared with replay
# ==========================================================================

@dataclass
class Position:
    trade_id: Optional[int]
    symbol: str
    side: str                    # 'long' | 'short'
    qty: int
    entry_price: float
    entry_ts: datetime
    last_bar_ts: Optional[datetime]   # the bar that triggered entry; later bars increment bars_held
    bars_held: int = 0
    mae_pct: float = 0.0         # <= 0: worst signed move from entry (adverse)
    mfe_pct: float = 0.0         # >= 0: best signed move from entry (favorable)
    entry_bid: Optional[float] = None
    entry_ask: Optional[float] = None

    def signed_move_pct(self, price: float) -> float:
        """Favorable-positive move from entry, in percent."""
        if self.side == "long":
            return (price - self.entry_price) / self.entry_price * 100.0
        return (self.entry_price - price) / self.entry_price * 100.0

    def on_bar(self, ts: datetime, close: float) -> bool:
        """Account for a completed bar. Returns True if it was a new bar (bars_held advanced)."""
        if self.last_bar_ts is not None and ts <= self.last_bar_ts:
            return False
        self.last_bar_ts = ts
        self.bars_held += 1
        self.update_excursion(close)
        return True

    def update_excursion(self, price: float) -> None:
        m = self.signed_move_pct(price)
        if m > self.mfe_pct:
            self.mfe_pct = m
        if m < self.mae_pct:
            self.mae_pct = m


def check_exit(pos: Position, close: Optional[float], vwap: Optional[float], params: Params) -> Optional[str]:
    """Exit decision on the latest completed bar. Order: vwap_touch, stop_loss, time_stop.
    eod_flatten is applied by the flatten routine, not here."""
    if close is None:
        return None
    if vwap is not None:
        if pos.side == "long" and close >= vwap:
            return "vwap_touch"
        if pos.side == "short" and close <= vwap:
            return "vwap_touch"
    if -pos.signed_move_pct(close) >= params.stop_pct:
        return "stop_loss"
    if pos.bars_held >= params.max_hold_bars:
        return "time_stop"
    return None


def half_spread(bid: Optional[float], ask: Optional[float]) -> float:
    if bid is None or ask is None or ask < bid:
        return 0.0
    return (ask - bid) / 2.0


def trade_economics(side: str, qty: int, entry_price: float, exit_price: float,
                    entry_bid: Optional[float], entry_ask: Optional[float],
                    exit_bid: Optional[float], exit_ask: Optional[float]) -> tuple[float, float, float]:
    """(gross_pnl, est_cost, net_pnl) for replay, whose fills are at the bar close.
    est_cost = (half spread at entry + half spread at exit) × qty + 0.01% × notional × 2."""
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    notional = entry_price * qty
    cost = (half_spread(entry_bid, entry_ask) + half_spread(exit_bid, exit_ask)) * qty + SLIPPAGE_RATE * notional * 2
    return gross, cost, gross - cost


def fill_vs_mid(price: float, bid: Optional[float], ask: Optional[float]) -> float:
    """|fill − quote mid| per share; 0 when the quote is missing or crossed."""
    if bid is None or ask is None or ask < bid:
        return 0.0
    return abs(price - (bid + ask) / 2.0)


def live_trade_economics(side: str, qty: int, entry_price: float, exit_price: float,
                         entry_bid: Optional[float], entry_ask: Optional[float],
                         exit_bid: Optional[float], exit_ask: Optional[float]) -> tuple[float, float, float]:
    """(gross_pnl, est_cost, net_pnl) for a live trade, whose prices are real broker fills.
    est_cost = (|entry fill − mid| + |exit fill − mid|) × qty + 0.01% × notional × 2, recorded as a
    diagnostic. Real fills already carry the spread, so net_pnl = gross − the slippage term only;
    subtracting the fill-vs-mid term as well would count it twice (DECISIONS.md)."""
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    slippage = SLIPPAGE_RATE * entry_price * qty * 2
    cost = (fill_vs_mid(entry_price, entry_bid, entry_ask) + fill_vs_mid(exit_price, exit_bid, exit_ask)) * qty \
        + slippage
    return gross, cost, gross - slippage


# ==========================================================================
# Engine
# ==========================================================================

@dataclass
class CycleResult:
    ts: datetime
    state: str
    symbols_evaluated: int = 0
    signals_fired: int = 0
    orders_placed: int = 0
    errors: int = 0
    duration_ms: int = 0
    cycle_id: Optional[int] = None
    reasons: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (f"cycle {self.ts.isoformat(timespec='seconds')} state={self.state} evaluated={self.symbols_evaluated} "
                f"fired={self.signals_fired} orders={self.orders_placed} errors={self.errors} "
                f"duration_ms={self.duration_ms} reasons={self.reasons}")


class Engine:
    def __init__(self, store: Store, broker, clock: MarketClock, universe: list[str], config_params: dict,
                 sleep_fn: Callable[[float], None] = time.sleep, sweep_fn: Optional[Callable] = None):
        self.store = store
        self.broker = broker
        self.clock = clock
        self.universe = list(universe)
        self.config_params = Params.from_dict(config_params)
        self.params: Params = self.config_params
        self.data = DataState(self.universe)
        self.positions: dict[str, Position] = {}
        self.exit_bar_count: dict[str, int] = {}   # symbol -> bar_count when it last exited (today)
        self.state = "idle"
        self._sleep = sleep_fn
        self._sweep_fn = sweep_fn
        self._stop = False
        # per-day bookkeeping
        self._data_day: Optional[date] = None
        self._preopen_done: Optional[date] = None
        self._flattened: Optional[date] = None
        self._swept: Optional[date] = None
        self.counters = {"cycles_today": 0, "signals_today": 0, "fired_today": 0,
                         "opened_today": 0, "closed_today": 0}

    # ---- helpers -----------------------------------------------------------
    def now(self) -> datetime:
        return self.clock.now_et()

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(state)
        if state != self.state:
            log.info("state %s -> %s", self.state, state)
        self.state = state
        self.store.write_heartbeat(state=state)

    def _log_error(self, where: str, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        log.error("%s: %s", where, msg)
        try:
            now = self.now()
            self.store.log_error(where, msg, traceback.format_exc(), now)
            self.store.write_heartbeat(last_error=f"{where}: {msg}"[:500], last_error_ts=now)
        except Exception:
            log.exception("could not write error to store")

    def load_params(self) -> Params:
        """Live params from the store; seeded from config.yaml the first time."""
        row = self.store.read_params()
        if row is None:
            self.store.write_params(self.config_params.to_dict(), "config", self.now())
            self.store.write_params_history(self.now().date(), self.config_params.to_dict(), "config", None)
            self.params = self.config_params
        else:
            try:
                p = Params.from_dict(row["params"])
                p.validate()
                self.params = p
            except (ValueError, TypeError) as e:
                self._log_error("params", e)
        return self.params

    def _init_counters_for(self, d: date) -> None:
        st = self.store
        self.counters = {
            "cycles_today": len(st.cycles_for_day(d)),
            "signals_today": st.signals_count_for_day(d),
            "fired_today": st.signals_count_for_day(d, fired_only=True),
            "opened_today": st.opened_count_for_day(d),
            "closed_today": len(st.closed_trades_for_day(d)),
        }

    def _net_pnl_today(self, d: date) -> float:
        return sum((t.net_pnl or 0.0) for t in self.store.closed_trades_for_day(d))

    def _write_heartbeat(self, last_cycle_ts: Optional[datetime] = None) -> None:
        d = self.now().date()
        fields = dict(state=self.state, open_positions=len(self.positions),
                      net_pnl_today=self._net_pnl_today(d), **self.counters)
        if last_cycle_ts is not None:
            fields["last_cycle_ts"] = last_cycle_ts
        self.store.write_heartbeat(**fields)

    # ---- startup / preopen -------------------------------------------------
    def startup(self) -> None:
        now = self.now()
        self.store.write_heartbeat(state="idle", pid=os.getpid(), started_at=now)
        self.load_params()
        self._init_counters_for(now.date())
        session = self.clock.today_session()
        if session is not None:
            self._ensure_data_day(session)
            try:
                self.seed_bars(session)
            except Exception as e:
                self._log_error("startup.seed_bars", e)
            self._rebuild_cooldowns(session)
        self.reconcile()
        self._write_heartbeat()

    def preopen(self, session: Session) -> None:
        self.set_state("preopen")
        self.load_params()
        self._ensure_data_day(session)
        self._init_counters_for(session.date)
        try:
            self.seed_bars(session)
        except Exception as e:
            self._log_error("preopen.seed_bars", e)
        self._rebuild_cooldowns(session)
        self.reconcile()
        self._preopen_done = session.date
        self._write_heartbeat()

    def _ensure_data_day(self, session: Session) -> None:
        if self._data_day != session.date:
            self.data.reset()
            self.exit_bar_count = {}
            self._data_day = session.date
            for sym, bars in self.store.bars_for_day(session.date).items():
                self.data.update(sym, bars)

    def _rebuild_cooldowns(self, session: Session) -> None:
        """After a restart: recover each symbol's bar count at its last exit today from closed trades.
        An exit at cycle hh:mm:05 acted on the bars before hh:mm:00."""
        for t in self.store.closed_trades_for_day(session.date):
            st = self.data.states.get(t.symbol)
            if st is None or not t.exit_ts:
                continue
            cutoff = datetime.fromisoformat(t.exit_ts).astimezone(ET).replace(second=0, microsecond=0)
            n = sum(1 for b in st.bars if b.ts < cutoff)
            self.exit_bar_count[t.symbol] = max(n, self.exit_bar_count.get(t.symbol, 0))

    def _bars_end_bound(self, now: datetime) -> datetime:
        """Exclude the minute that is still forming."""
        return now.replace(second=0, microsecond=0) - timedelta(seconds=1)

    def seed_bars(self, session: Session) -> None:
        """Fetch every bar since session open (or since the last stored bar) and persist."""
        now = self.now()
        if now < session.open:
            return
        start = self.data.last_ts()
        start = (start + timedelta(minutes=1)) if start else session.open
        end = min(self._bars_end_bound(now), session.close)
        if start > end:
            return
        fetched = self.broker.bars_1m(self.universe, start, end)
        for sym, bars in fetched.items():
            added = self.data.update(sym, bars)
            self.store.write_bars(sym, added)

    # ---- reconcile ---------------------------------------------------------
    def reconcile(self) -> dict:
        """Match broker positions against open trades in the store. See spec §8."""
        now = self.now()
        result = {"adopted": [], "orphans": [], "missing": []}
        try:
            broker_pos = {p["symbol"]: p for p in self.broker.positions() if abs(p["qty"]) > 0}
        except BrokerError as e:
            self._log_error("reconcile", e)
            return result
        open_trades = self.store.open_trades()
        self.positions = {}
        for t in open_trades:
            bp = broker_pos.get(t.symbol)
            match = bp is not None and ((t.side == "long") == (bp["qty"] > 0))
            if match:
                pos = self._adopt(t)
                self.positions[t.symbol] = pos
                del broker_pos[t.symbol]
                result["adopted"].append(t.symbol)
                log.info("reconcile: adopted %s %s x%s", t.side, t.symbol, t.qty)
            else:
                # open trade in store, no broker position → close the record
                st = self.data.states.get(t.symbol)
                px = (st.last_close if st and st.last_close else None) or t.entry_price
                gross, cost, net = live_trade_economics(t.side, t.qty, t.entry_price, px, t.entry_bid, t.entry_ask, None, None)
                self.store.close_trade(t.id, now, px, None, None, None, "reconcile_missing",
                                       t.bars_held, t.mae_pct, t.mfe_pct, gross, cost, net)
                self.counters["closed_today"] += 1
                self.store.log_error("reconcile", f"open trade {t.id} {t.side} {t.symbol} x{t.qty} has no broker "
                                     f"position; closed as reconcile_missing at {px}", None, now)
                result["missing"].append(t.symbol)
        for sym, bp in broker_pos.items():
            # broker position with no open trade → orphan: flatten immediately
            qty = int(abs(bp["qty"]))
            side = "sell" if bp["qty"] > 0 else "buy"
            try:
                oid = self.broker.submit_market(sym, qty, side)
                fill = self.wait_fill(oid)
                self.store.log_error("reconcile", f"orphan position {sym} qty {bp['qty']} flattened "
                                     f"(order {oid}, status {fill.get('status')})", None, now)
            except BrokerError as e:
                self.store.log_error("reconcile", f"orphan position {sym} qty {bp['qty']} could not be "
                                     f"flattened: {e}", None, now)
            result["orphans"].append(sym)
        return result

    def _adopt(self, t: Trade) -> Position:
        entry_ts = datetime.fromisoformat(t.entry_ts).astimezone(ET)
        pos = Position(t.id, t.symbol, t.side, t.qty, t.entry_price, entry_ts, None,
                       bars_held=t.bars_held, mae_pct=t.mae_pct, mfe_pct=t.mfe_pct,
                       entry_bid=t.entry_bid, entry_ask=t.entry_ask)
        # Rebuild bars_held / excursions from bars completed after entry, when we have them.
        st = self.data.states.get(t.symbol)
        entry_minute = entry_ts.replace(second=0, microsecond=0)
        after = [b for b in (st.bars if st else []) if b.ts >= entry_minute]
        if after:
            pos.bars_held = 0
            pos.last_bar_ts = None
            for b in after:
                pos.on_bar(b.ts, b.c)
            pos.mae_pct = min(pos.mae_pct, t.mae_pct)
            pos.mfe_pct = max(pos.mfe_pct, t.mfe_pct)
        elif st and st.last_ts:
            pos.last_bar_ts = st.last_ts
        return pos

    # ---- orders ------------------------------------------------------------
    def wait_fill(self, order_id: str, timeout: float = FILL_TIMEOUT_S) -> dict:
        """Poll order_status every FILL_POLL_S until terminal or `timeout` seconds of polling
        (timeout / FILL_POLL_S polls). Returns the last status dict."""
        polls = max(1, int(timeout / FILL_POLL_S))
        last: dict = {"status": "unknown", "filled_qty": 0.0, "filled_avg_price": None, "filled_at": None}
        for i in range(polls):
            last = self.broker.order_status(order_id)
            if last.get("status") in TERMINAL_STATUSES:
                return last
            if i < polls - 1:
                self._sleep(FILL_POLL_S)
        return last

    def settle_order(self, order_id: str) -> dict:
        """Wait FILL_TIMEOUT_S, then up to FILL_SETTLE_S more, for a terminal status. If the order is still
        working, cancel it and wait for the cancel to land, so filled_qty / filled_avg_price are final.
        The result is non-terminal only if the cancel itself failed."""
        fill = self.wait_fill(order_id)
        if fill.get("status") in TERMINAL_STATUSES:
            return fill
        fill = self.wait_fill(order_id, FILL_SETTLE_S)
        if fill.get("status") in TERMINAL_STATUSES:
            return fill
        try:
            self.broker.cancel_order(order_id)
        except BrokerError:
            pass  # already in store.errors; the status below stays non-terminal
        return self.wait_fill(order_id)

    def open_position(self, sig: Signal, quotes: dict, now: datetime) -> Optional[Position]:
        """Returns None when the settled entry order filled nothing."""
        sym = sig.symbol
        qty = position_qty(sig.price, self.params)
        q = quotes.get(sym) or {}
        oid = self.broker.submit_market(sym, qty, "buy" if sig.side == "long" else "sell")
        fill = self.settle_order(oid)
        status = fill.get("status")
        filled_qty = int(fill.get("filled_qty") or 0)
        if status not in TERMINAL_STATUSES:
            # the cancel failed and the order may still fill: track the full size; reconcile/flatten catch the rest
            self.store.log_error("cycle.entry", f"{sym} entry order {oid} still {status} after cancel attempt "
                                 f"({filled_qty}/{qty} filled); recorded x{qty}", None, now)
        elif filled_qty == 0:
            self.store.log_error("cycle.entry", f"{sym} entry order {oid} {status} with nothing filled; "
                                 f"no trade opened", None, now)
            return None
        elif filled_qty < qty:
            self.store.log_error("cycle.entry", f"{sym} entry order {oid} {status} after {filled_qty}/{qty} "
                                 f"filled; trade opened x{filled_qty}", None, now)
            qty = filled_qty
        entry_price = fill.get("filled_avg_price") or sig.price
        tid = self.store.open_trade(sym, sig.side, qty, now, entry_price, oid, q.get("bid"), q.get("ask"),
                                    hypothesis(sig, self.params), self.params.to_dict())
        pos = Position(tid, sym, sig.side, qty, entry_price, now, self.data[sym].last_ts,
                       entry_bid=q.get("bid"), entry_ask=q.get("ask"))
        self.positions[sym] = pos
        self.counters["opened_today"] += 1
        log.info("OPEN %s %s x%d @ %.4f (%s)", sig.side, sym, qty, entry_price, hypothesis(sig, self.params))
        return pos

    def close_position(self, pos: Position, reason: str, quotes: dict, now: datetime) -> None:
        sym = pos.symbol
        q = quotes.get(sym) or {}
        oid = self.broker.submit_market(sym, pos.qty, "sell" if pos.side == "long" else "buy")
        fill = self.settle_order(oid)
        status = fill.get("status")
        filled_qty = int(fill.get("filled_qty") or 0)
        st = self.data.states.get(sym)
        last_close = st.last_close if st and st.last_close else None
        if status not in TERMINAL_STATUSES:
            # the cancel failed and the order may still fill: close the whole record rather than risk a second
            # exit order next cycle; the flatten safety net closes any remainder
            exit_price = fill.get("filled_avg_price") or last_close or pos.entry_price
            self.store.log_error("cycle.exit", f"{sym} exit order {oid} still {status} after cancel attempt "
                                 f"({filled_qty}/{pos.qty} filled); recorded x{pos.qty} at {exit_price}", None, now)
            self._record_close(pos, reason, exit_price, oid, q.get("bid"), q.get("ask"), now)
        elif filled_qty >= pos.qty:
            self._record_close(pos, reason, fill["filled_avg_price"], oid, q.get("bid"), q.get("ask"), now)
        elif filled_qty == 0:
            # nothing sold: the position stays open and the exit is retried next cycle
            self.store.log_error("cycle.exit", f"{sym} exit order {oid} {status} with nothing filled; "
                                 f"position stays open", None, now)
        else:
            # close the filled part as its own trade; the remainder stays open and is retried next cycle
            rest = pos.qty - filled_qty
            rest_id = self.store.split_trade(pos.trade_id, filled_qty) if pos.trade_id is not None else None
            self.store.log_error("cycle.exit", f"{sym} exit order {oid} {status} after {filled_qty}/{pos.qty} "
                                 f"filled; closed x{filled_qty}, x{rest} stays open as trade {rest_id}", None, now)
            remainder = replace(pos, trade_id=rest_id, qty=rest)
            pos.qty = filled_qty
            self._record_close(pos, reason, fill["filled_avg_price"], oid, q.get("bid"), q.get("ask"), now)
            self.positions[sym] = remainder

    def _record_close(self, pos: Position, reason: str, exit_price: float, oid: Optional[str],
                      exit_bid: Optional[float], exit_ask: Optional[float], now: datetime) -> None:
        pos.update_excursion(exit_price)
        gross, cost, net = live_trade_economics(pos.side, pos.qty, pos.entry_price, exit_price,
                                                pos.entry_bid, pos.entry_ask, exit_bid, exit_ask)
        if pos.trade_id is not None:
            self.store.close_trade(pos.trade_id, now, exit_price, oid, exit_bid, exit_ask, reason,
                                   pos.bars_held, pos.mae_pct, pos.mfe_pct, gross, cost, net)
        self.positions.pop(pos.symbol, None)
        st = self.data.states.get(pos.symbol)
        self.exit_bar_count[pos.symbol] = st.bar_count if st else 0
        self.counters["closed_today"] += 1
        log.info("CLOSE %s %s x%d @ %.4f %s bars=%d gross=%.2f cost=%.2f net=%.2f", pos.side, pos.symbol,
                 pos.qty, exit_price, reason, pos.bars_held, gross, cost, net)

    # ---- the cycle ---------------------------------------------------------
    def run_cycle(self, now: Optional[datetime] = None) -> CycleResult:
        """One full cycle. Never raises: every step is caught, logged and counted."""
        t0 = time.monotonic()
        now = now or self.now()
        res = CycleResult(ts=now, state=self.state)
        session = self.clock.today_session()
        paused = False

        # 1. params + controls
        try:
            self.load_params()
            controls = self.store.read_controls()
            paused = controls["paused"]
            if controls["flatten_requested"]:
                self.store.clear_flatten_request(now)
                log.info("flatten requested from dashboard")
                res.orders_placed += self.flatten("manual_flatten", now)
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.controls", e)

        # 2. bars
        bars_ok = False
        try:
            if session is not None:
                self._ensure_data_day(session)
                if now >= session.open:
                    start = self.data.last_ts()
                    start = (start + timedelta(minutes=1)) if start else session.open
                    end = min(self._bars_end_bound(now), session.close)
                    if start <= end:
                        fetched = self.broker.bars_1m(self.universe, start, end)
                        for sym, bars in fetched.items():
                            added = self.data.update(sym, bars)
                            self.store.write_bars(sym, added)
            bars_ok = True
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.bars", e)

        # 3. quotes (non-fatal: cost estimate falls back to zero spread)
        quotes: dict = {}
        try:
            quotes = self.broker.quotes(self.universe) or {}
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.quotes", e)

        rows: list[dict] = []
        if bars_ok:
            # 4. exits
            for pos in list(self.positions.values()):
                try:
                    st = self.data.states.get(pos.symbol)
                    if st is not None and st.last_ts is not None:
                        pos.on_bar(st.last_ts, st.last_close)
                        if pos.trade_id is not None:
                            self.store.update_trade_progress(pos.trade_id, pos.bars_held, pos.mae_pct, pos.mfe_pct)
                        reason = check_exit(pos, st.last_close, st.vwap, self.params)
                        if reason:
                            self.close_position(pos, reason, quotes, now)
                            res.orders_placed += 1
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.exit", e)

            # 5. entries — every symbol is evaluated and logged every cycle
            entries_ok = session is not None and session.entries_allowed(now) and not paused
            params_dict = self.params.to_dict()
            for sym in self.universe:
                try:
                    st = self.data[sym]
                    slots = len(self.positions) < self.params.max_open_positions
                    sig = evaluate(st, self.params, sym in self.positions, slots, entries_ok,
                                   bars_since(st, self.exit_bar_count.get(sym)))
                    res.symbols_evaluated += 1
                    res.reasons[sig.reason] = res.reasons.get(sig.reason, 0) + 1
                    row = sig.to_row()
                    row["ts"] = now
                    row["params"] = params_dict
                    rows.append(row)
                    if sig.fired:
                        res.signals_fired += 1
                        self.open_position(sig, quotes, now)
                        res.orders_placed += 1
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.entry", e)

        # 6. cycle row + signals + heartbeat
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        try:
            res.cycle_id = self.store.write_cycle(now, self.state, res.symbols_evaluated, res.signals_fired,
                                                  res.orders_placed, res.errors, res.duration_ms)
            self.store.write_signals(res.cycle_id, rows)
            self.counters["cycles_today"] += 1
            self.counters["signals_today"] += len(rows)
            self.counters["fired_today"] += res.signals_fired
            self._write_heartbeat(last_cycle_ts=now)
        except Exception as e:
            res.errors += 1
            self._log_error("cycle.write", e)
        log.info("%s", res)
        return res

    # ---- flatten -----------------------------------------------------------
    def flatten(self, reason: str = "eod_flatten", now: Optional[datetime] = None) -> int:
        """Close every open trade, then close_all_positions() as a safety net, then cancel_all().
        Returns the number of orders placed. Never raises."""
        now = now or self.now()
        orders = 0
        quotes: dict = {}
        try:
            quotes = self.broker.quotes(self.universe) or {}
        except Exception as e:
            self._log_error("flatten.quotes", e)
        for pos in list(self.positions.values()):
            try:
                self.close_position(pos, reason, quotes, now)
                orders += 1
            except Exception as e:
                self._log_error("flatten.exit", e)
        # safety net: anything the broker still holds
        try:
            remaining = [p for p in self.broker.positions() if abs(p["qty"]) > 0]
            self.broker.close_all_positions()
            for p in remaining:
                sym = p["symbol"]
                px = p.get("current_price") or 0.0
                pos = self.positions.get(sym)
                if pos is not None:
                    self._record_close(pos, "eod_safety_net", px or pos.entry_price, None, None, None, now)
                else:
                    # a store trade whose close_position failed above, or an orphan
                    tr = next((t for t in self.store.open_trades() if t.symbol == sym), None)
                    if tr is not None:
                        gross, cost, net = live_trade_economics(tr.side, tr.qty, tr.entry_price, px or tr.entry_price,
                                                                tr.entry_bid, tr.entry_ask, None, None)
                        self.store.close_trade(tr.id, now, px or tr.entry_price, None, None, None, "eod_safety_net",
                                               tr.bars_held, tr.mae_pct, tr.mfe_pct, gross, cost, net)
                        self.counters["closed_today"] += 1
                self.store.log_error("flatten", f"safety net closed {sym} qty {p['qty']} (eod_safety_net)", None, now)
                orders += 1
        except Exception as e:
            self._log_error("flatten.safety_net", e)
        try:
            self.broker.cancel_all()
        except Exception as e:
            self._log_error("flatten.cancel_all", e)
        self.positions = {k: v for k, v in self.positions.items() if v.trade_id is not None
                          and self.store.get_trade(v.trade_id) and self.store.get_trade(v.trade_id).is_open}
        self._write_heartbeat()
        return orders

    # ---- sweep -------------------------------------------------------------
    def run_sweep(self, session: Session) -> Optional[dict]:
        from .sweep import run_sweep
        fn = self._sweep_fn or run_sweep
        try:
            sessions = {}
            if self._sweep_fn is None:
                dates = self.store.bar_dates(5)
                if dates:
                    d0 = date.fromisoformat(dates[0])
                    sessions = self.clock.sessions_between(d0 - timedelta(days=1), session.date)
            summary = fn(self.store, self.params, session.date, self.now(), sessions=sessions) if sessions \
                else fn(self.store, self.params, session.date, self.now())
            log.info("sweep done: %s", summary.get("reason") if isinstance(summary, dict) else summary)
            return summary
        except Exception as e:
            self._log_error("sweep", e)
            return None

    # ---- main loop ---------------------------------------------------------
    def stop(self) -> None:
        self._stop = True

    def _wait_until(self, target: datetime, heartbeat_every: float = 30.0) -> None:
        """Sleep in chunks until target, refreshing the heartbeat state (not last_cycle_ts)."""
        while not self._stop:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, heartbeat_every))
            self.store.write_heartbeat(state=self.state)

    def next_cycle_time(self, now: Optional[datetime] = None) -> datetime:
        now = now or self.now()
        target = now.replace(second=CYCLE_OFFSET_S, microsecond=0)
        if target <= now:
            target += timedelta(minutes=1)
        return target

    def run_forever(self) -> None:
        self.startup()
        while not self._stop:
            try:
                self.tick()
            except Exception as e:  # nothing should ever get here
                self._log_error("unhandled", e)
                self._sleep(5)

    def tick(self) -> None:
        """One step of the state machine. Returns after at most ~1 minute of waiting."""
        now = self.now()
        session = self.clock.today_session()
        if session is None or now >= session.sweep_at and self._swept == session.date:
            self.set_state("idle")
            nxt = self.clock.next_session()
            target = nxt.preopen_at if nxt else now + timedelta(hours=1)
            self._wait_until(min(target, now + timedelta(minutes=10)))
            return
        if now < session.preopen_at:
            self.set_state("idle")
            self._wait_until(session.preopen_at)
            return
        if now < session.flatten_at:
            if self._preopen_done != session.date:
                self.preopen(session)
            if now < session.open:
                self.set_state("preopen")
                self._wait_until(session.open)
                return
            self.set_state("running")
            target = self.next_cycle_time(now)
            if target >= session.flatten_at:
                self._wait_until(session.flatten_at)
                return
            self._wait_until(target)
            if not self._stop:
                self.run_cycle()
            return
        if self._flattened != session.date:
            self.set_state("flattening")
            self.flatten("eod_flatten")
            self._flattened = session.date
            self.set_state("postclose")
            return
        if now < session.sweep_at:
            self.set_state("postclose")
            self._wait_until(session.sweep_at)
            return
        if self._swept != session.date:
            # `_swept` is lost on restart; the params_history row is the durable record that the
            # sweep ran, so a restart after sweep_at must not sweep (and step params) a second time.
            if self.store.has_sweep_for(session.date):
                log.info("sweep for %s already in params_history; skipping", session.date)
            else:
                self.set_state("sweeping")
                self.run_sweep(session)
            self._swept = session.date
            self.set_state("idle")
            return
