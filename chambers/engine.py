"""Engine: cycle loop, positions, exits, flatten, reconcile, state machine.

The pure pieces at the top of this module (`Position`, `check_exit`,
`trade_economics`) are the single exit/accounting code path. `replay.py`
imports them so replay and live can never drift apart. Live trades are costed
with `live_trade_economics` instead, because their prices are real fills.

`SleeveBase` holds what every live sleeve shares since Phase 1A: order
execution with the Phase 0 partial-fill handling, position closing, the
per-sleeve heartbeat, the post-signal entry gates (news day, portfolio risk)
and reconcile. `Engine` is sleeve S0 (the Phase 0 VWAP reversion engine),
unchanged in behaviour when it runs alone; `sleeve.BarSleeve` runs S1–S3.

States: idle → preopen → running → flattening → postclose → sweeping → idle

`plan(now)` returns (when, action) without waiting, so one scheduler can
interleave several sleeves; `tick()` = plan, wait, act — the Phase 0 loop.
"""
from __future__ import annotations

import logging
import os
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .broker import BrokerError, is_crypto
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
LATE_CYCLE_S = 50            # a minute's cycle may still run this late if the previous minute ran (1A scheduler)
SLIPPAGE_RATE = 0.0001       # 0.01% of notional, applied twice (entry + exit)
QTY_TOL = 1e-6


# ==========================================================================
# Pure, shared with replay
# ==========================================================================

@dataclass
class Position:
    trade_id: Optional[int]
    symbol: str
    side: str                    # 'long' | 'short'
    qty: float
    entry_price: float
    entry_ts: datetime
    last_bar_ts: Optional[datetime]   # the bar that triggered entry; later bars increment bars_held
    bars_held: int = 0
    mae_pct: float = 0.0         # <= 0: worst signed move from entry (adverse)
    mfe_pct: float = 0.0         # >= 0: best signed move from entry (favorable)
    entry_bid: Optional[float] = None
    entry_ask: Optional[float] = None
    stop_price: Optional[float] = None   # S1–S3: ATR stop fixed at entry
    extra: dict = field(default_factory=dict)

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


def trade_economics(side: str, qty: float, entry_price: float, exit_price: float,
                    entry_bid: Optional[float], entry_ask: Optional[float],
                    exit_bid: Optional[float], exit_ask: Optional[float],
                    fee_rate: float = 0.0) -> tuple[float, float, float]:
    """(gross_pnl, est_cost, net_pnl) for replay, whose fills are at the bar close.
    est_cost = (half spread at entry + half spread at exit) × qty + 0.01% × notional × 2
    (+ fee_rate × (entry + exit notional) for crypto, S2)."""
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    notional = entry_price * qty
    cost = (half_spread(entry_bid, entry_ask) + half_spread(exit_bid, exit_ask)) * qty + SLIPPAGE_RATE * notional * 2
    cost += fee_rate * (entry_price + exit_price) * qty
    return gross, cost, gross - cost


def fill_vs_mid(price: float, bid: Optional[float], ask: Optional[float]) -> float:
    """|fill − quote mid| per share; 0 when the quote is missing or crossed."""
    if bid is None or ask is None or ask < bid:
        return 0.0
    return abs(price - (bid + ask) / 2.0)


def live_trade_economics(side: str, qty: float, entry_price: float, exit_price: float,
                         entry_bid: Optional[float], entry_ask: Optional[float],
                         exit_bid: Optional[float], exit_ask: Optional[float],
                         fee_rate: float = 0.0) -> tuple[float, float, float]:
    """(gross_pnl, est_cost, net_pnl) for a live trade, whose prices are real broker fills.
    est_cost = (|entry fill − mid| + |exit fill − mid|) × qty + 0.01% × notional × 2, recorded as a
    diagnostic. Real fills already carry the spread, so net_pnl = gross − the slippage term only;
    subtracting the fill-vs-mid term as well would count it twice (DECISIONS.md).
    `fee_rate` (crypto, S2): the broker's commission on entry and exit notional, which fills do not
    carry, so it is subtracted from net and included in est_cost."""
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    slippage = SLIPPAGE_RATE * entry_price * qty * 2
    fees = fee_rate * (entry_price + exit_price) * qty
    cost = (fill_vs_mid(entry_price, entry_bid, entry_ask) + fill_vs_mid(exit_price, exit_bid, exit_ask)) * qty \
        + slippage + fees
    return gross, cost, gross - slippage - fees


# ==========================================================================
# Shared live-sleeve machinery
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
    sleeve_id: str = "S0"

    def __str__(self) -> str:
        return (f"cycle {self.sleeve_id} {self.ts.isoformat(timespec='seconds')} state={self.state} "
                f"evaluated={self.symbols_evaluated} fired={self.signals_fired} orders={self.orders_placed} "
                f"errors={self.errors} duration_ms={self.duration_ms} reasons={self.reasons}")


class SleeveBase:
    """What every live sleeve shares. Subclasses set the class attributes and implement
    `last_price`, `adopt`, `on_exit_recorded`, `plan`, `startup`."""

    sleeve_id = "S0"
    eod_flatten = True           # S0, S1: flattened at flatten_at; S2, S3 carry positions
    equity_sleeve = True         # subject to the equity-session daily loss halt (§3.2)
    fee_rate = 0.0               # crypto commission rate (S2)

    def _init_base(self, store: Store, broker, clock: MarketClock, sleep_fn: Callable[[float], None]) -> None:
        self.store = store
        self.broker = broker
        self.clock = clock
        self._sleep = sleep_fn
        self.positions: dict[str, Position] = {}
        self.exit_bar_count: dict[str, int] = {}
        self.state = "idle"
        self._stop = False
        self.portfolio = None            # risk.Portfolio (set by the scheduler)
        self.coordinator = None          # scheduler.Coordinator: cross-sleeve reconcile / flatten
        self.twin = None                 # twin.Twin (set by the scheduler)
        self.handle_flatten_requests = True   # False under the scheduler, which flattens every sleeve
        self._news_cache: dict[str, tuple[bool, Optional[str]]] = {}
        self.counters = {"cycles_today": 0, "signals_today": 0, "fired_today": 0,
                         "opened_today": 0, "closed_today": 0}

    # ---- helpers -----------------------------------------------------------
    def now(self) -> datetime:
        return self.clock.now_et()

    def where(self, where: str) -> str:
        """errors.where_ value; S0 keeps the Phase 0 names, other sleeves are prefixed."""
        return where if self.sleeve_id == "S0" else f"{self.sleeve_id}.{where}"

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(state)
        if state != self.state:
            log.info("%s state %s -> %s", self.sleeve_id, self.state, state)
        self.state = state
        self.store.write_heartbeat(self.sleeve_id, state=state)

    def _log_error(self, where: str, exc: BaseException) -> None:
        where = self.where(where)
        msg = f"{type(exc).__name__}: {exc}"
        log.error("%s: %s", where, msg)
        try:
            now = self.now()
            self.store.log_error(where, msg, traceback.format_exc(), now)
            self.store.write_heartbeat(self.sleeve_id, last_error=f"{where}: {msg}"[:500], last_error_ts=now)
        except Exception:
            log.exception("could not write error to store")

    def _note(self, where: str, message: str, now: datetime) -> None:
        self.store.log_error(self.where(where), message, None, now)

    def _qty(self, x) -> float:
        """Order/position quantity: whole shares for equities, fractional (9 dp) for crypto."""
        x = float(x or 0.0)
        return round(x, 9) if self.fee_rate or getattr(self, "fractional", False) else int(x)

    def news(self, d: date | str) -> tuple[bool, Optional[str]]:
        """(news_day, event) for an ET date from the econ calendar in the store (§5.4)."""
        key = d if isinstance(d, str) else d.isoformat()
        if key not in self._news_cache:
            ev = self.store.econ_events_on(key)
            self._news_cache[key] = (bool(ev), "; ".join(e["event"] for e in ev) or None)
        return self._news_cache[key]

    def _init_counters_for(self, d: date) -> None:
        st, sid = self.store, self.sleeve_id
        self.counters = {
            "cycles_today": len(st.cycles_for_day(d, sid)),
            "signals_today": st.signals_count_for_day(d, sleeve_id=sid),
            "fired_today": st.signals_count_for_day(d, fired_only=True, sleeve_id=sid),
            "opened_today": st.opened_count_for_day(d, sid),
            "closed_today": len(st.closed_trades_for_day(d, sid)),
        }

    def _net_pnl_today(self, d: date) -> float:
        return sum((t.net_pnl or 0.0) for t in self.store.closed_trades_for_day(d, self.sleeve_id))

    def _write_heartbeat(self, last_cycle_ts: Optional[datetime] = None) -> None:
        d = self.now().date()
        fields = dict(state=self.state, open_positions=len(self.positions),
                      net_pnl_today=self._net_pnl_today(d), **self.counters)
        if last_cycle_ts is not None:
            fields["last_cycle_ts"] = last_cycle_ts
        self.store.write_heartbeat(self.sleeve_id, **fields)

    def stop(self) -> None:
        self._stop = True

    # ---- subclass hooks ------------------------------------------------------
    def last_price(self, symbol: str) -> Optional[float]:
        raise NotImplementedError

    def adopt(self, t: Trade) -> Position:
        raise NotImplementedError

    def on_exit_recorded(self, pos: Position) -> None:
        pass

    def trade_economics(self, side, qty, entry_price, exit_price, eb, ea, xb, xa) -> tuple[float, float, float]:
        return live_trade_economics(side, qty, entry_price, exit_price, eb, ea, xb, xa, self.fee_rate)

    # ---- entry gates (after the strategy fired) --------------------------------
    def entry_gate(self, symbol: str, side: str, notional: float, skip_news_days: bool,
                   now: datetime) -> Optional[str]:
        """Reason an entry the strategy fired must be skipped, or None. Order: news_day,
        daily_loss_halt, same_side_cap, exposure_cap (the last three are portfolio-wide, §3.2)."""
        if skip_news_days and self.news(now.date())[0]:
            return "news_day"
        if self.portfolio is not None:
            return self.portfolio.allow_entry(self, symbol, side, notional, now)
        return None

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

    def held_net(self, symbol: str) -> float:
        """Signed quantity the books say the broker holds for `symbol`: every sleeve's open trades."""
        return sum(t.signed_qty for t in self.store.open_trades(None) if t.symbol == symbol)

    def execute(self, symbol: str, qty: float, side: str) -> tuple[dict, str]:
        """Submit a market order and settle it. When another sleeve holds the symbol the other way and this
        order would cross the broker's net position through zero, it is sent as two orders (close the
        held side, then the rest), because a single order may not flip a position. Returns (fill, order id)
        with the fills combined; a single order behaves exactly as in Phase 0."""
        held = self.held_net(symbol)
        signed = qty if side == "buy" else -qty
        if held and held * (held + signed) < 0 and abs(held) > QTY_TOL:
            first = self._qty(abs(held))
            oid1 = self.broker.submit_market(symbol, first, side)
            f1 = self.settle_order(oid1)
            q1 = float(f1.get("filled_qty") or 0.0)
            if f1.get("status") not in TERMINAL_STATUSES or q1 + QTY_TOL < first:
                return f1, oid1
            rest = self._qty(qty - first)
            oid2 = self.broker.submit_market(symbol, rest, side)
            f2 = self.settle_order(oid2)
            q2 = float(f2.get("filled_qty") or 0.0)
            tot = q1 + q2
            avg = ((f1.get("filled_avg_price") or 0.0) * q1 + (f2.get("filled_avg_price") or 0.0) * q2) / tot \
                if tot else None
            return {"status": f2.get("status"), "filled_qty": tot, "filled_avg_price": avg,
                    "filled_at": f2.get("filled_at")}, f"{oid1}+{oid2}"
        oid = self.broker.submit_market(symbol, qty, side)
        return self.settle_order(oid), oid

    def enter(self, symbol: str, side: str, qty: float, ref_price: float, now: datetime) -> Optional[tuple]:
        """Place and settle an entry. Returns (qty, entry_price, order_id), or None when nothing filled."""
        fill, oid = self.execute(symbol, qty, "buy" if side == "long" else "sell")
        status = fill.get("status")
        filled_qty = self._qty(fill.get("filled_qty") or 0)
        if status not in TERMINAL_STATUSES:
            # the cancel failed and the order may still fill: track the full size; reconcile/flatten catch the rest
            self._note("cycle.entry", f"{symbol} entry order {oid} still {status} after cancel attempt "
                       f"({filled_qty}/{qty} filled); recorded x{qty}", now)
        elif filled_qty <= 0:
            self._note("cycle.entry", f"{symbol} entry order {oid} {status} with nothing filled; "
                       f"no trade opened", now)
            return None
        elif filled_qty + QTY_TOL < qty:
            self._note("cycle.entry", f"{symbol} entry order {oid} {status} after {filled_qty}/{qty} "
                       f"filled; trade opened x{filled_qty}", now)
            qty = filled_qty
        return qty, fill.get("filled_avg_price") or ref_price, oid

    def exit_qty(self, pos: Position) -> float:
        return pos.qty

    def close_position(self, pos: Position, reason: str, quotes: dict, now: datetime) -> None:
        sym = pos.symbol
        q = quotes.get(sym) or {}
        order_qty = self.exit_qty(pos)
        fill, oid = self.execute(sym, order_qty, "sell" if pos.side == "long" else "buy")
        status = fill.get("status")
        filled_qty = self._qty(fill.get("filled_qty") or 0)
        last_close = self.last_price(sym)
        if status not in TERMINAL_STATUSES:
            # the cancel failed and the order may still fill: close the whole record rather than risk a second
            # exit order next cycle; the flatten safety net closes any remainder
            exit_price = fill.get("filled_avg_price") or last_close or pos.entry_price
            self._note("cycle.exit", f"{sym} exit order {oid} still {status} after cancel attempt "
                       f"({filled_qty}/{pos.qty} filled); recorded x{pos.qty} at {exit_price}", now)
            self._record_close(pos, reason, exit_price, oid, q.get("bid"), q.get("ask"), now)
        elif filled_qty + QTY_TOL >= order_qty:
            self._record_close(pos, reason, fill["filled_avg_price"], oid, q.get("bid"), q.get("ask"), now)
        elif filled_qty <= 0:
            # nothing sold: the position stays open and the exit is retried next cycle
            self._note("cycle.exit", f"{sym} exit order {oid} {status} with nothing filled; "
                       f"position stays open", now)
        else:
            # close the filled part as its own trade; the remainder stays open and is retried next cycle
            rest = self._qty(pos.qty - filled_qty)
            rest_id = self.store.split_trade(pos.trade_id, filled_qty) if pos.trade_id is not None else None
            self._note("cycle.exit", f"{sym} exit order {oid} {status} after {filled_qty}/{pos.qty} "
                       f"filled; closed x{filled_qty}, x{rest} stays open as trade {rest_id}", now)
            remainder = replace(pos, trade_id=rest_id, qty=rest, extra=dict(pos.extra))
            pos.qty = filled_qty
            self._record_close(pos, reason, fill["filled_avg_price"], oid, q.get("bid"), q.get("ask"), now)
            self.positions[sym] = remainder

    def _record_close(self, pos: Position, reason: str, exit_price: float, oid: Optional[str],
                      exit_bid: Optional[float], exit_ask: Optional[float], now: datetime) -> None:
        pos.update_excursion(exit_price)
        gross, cost, net = self.trade_economics(pos.side, pos.qty, pos.entry_price, exit_price,
                                                pos.entry_bid, pos.entry_ask, exit_bid, exit_ask)
        if pos.trade_id is not None:
            self.store.close_trade(pos.trade_id, now, exit_price, oid, exit_bid, exit_ask, reason,
                                   pos.bars_held, pos.mae_pct, pos.mfe_pct, gross, cost, net)
        self.positions.pop(pos.symbol, None)
        self.on_exit_recorded(pos)
        self.counters["closed_today"] += 1
        log.info("CLOSE %s %s %s x%s @ %.4f %s bars=%d gross=%.2f cost=%.2f net=%.2f", self.sleeve_id, pos.side,
                 pos.symbol, pos.qty, exit_price, reason, pos.bars_held, gross, cost, net)

    def close_missing(self, t: Trade, now: datetime) -> None:
        """Reconcile: an open trade in the store with no broker position behind it."""
        px = self.last_price(t.symbol) or t.entry_price
        gross, cost, net = self.trade_economics(t.side, t.qty, t.entry_price, px, t.entry_bid, t.entry_ask,
                                                None, None)
        self.store.close_trade(t.id, now, px, None, None, None, "reconcile_missing",
                               t.bars_held, t.mae_pct, t.mfe_pct, gross, cost, net)
        self.counters["closed_today"] += 1
        self.store.log_error("reconcile", f"open trade {t.id} {t.side} {t.symbol} x{t.qty} has no broker "
                             f"position; closed as reconcile_missing at {px}", None, now)

    # ---- reconcile ---------------------------------------------------------
    def reconcile(self) -> dict:
        """Match broker positions against open trades (spec §8, per sleeve since 1A). Under the scheduler
        the coordinator reconciles every sleeve together, because the broker holds their sum."""
        from .reconcile import reconcile_sleeves
        if self.coordinator is not None:
            return self.coordinator.reconcile()
        return reconcile_sleeves([self], self.store, self.broker, self.now(), self._log_error)

    # ---- flatten -----------------------------------------------------------
    def flatten(self, reason: str = "eod_flatten", now: Optional[datetime] = None,
                safety_net: bool = True) -> int:
        """Close every open trade of this sleeve, then (safety_net) close whatever the broker still holds that
        no other sleeve's open trades explain, then cancel_all(). Returns the number of orders placed.
        With no other sleeve holding anything, the safety net is Phase 0's close_all_positions(). Never raises."""
        now = now or self.now()
        orders = 0
        quotes: dict = {}
        try:
            quotes = self.quotes_for(list(self.positions)) or {}
        except Exception as e:
            self._log_error("flatten.quotes", e)
        for pos in list(self.positions.values()):
            try:
                self.close_position(pos, reason, quotes, now)
                orders += 1
            except Exception as e:
                self._log_error("flatten.exit", e)
        if safety_net:
            orders += self._safety_net(now)
            try:
                self.broker.cancel_all()
            except Exception as e:
                self._log_error("flatten.cancel_all", e)
        self.positions = {k: v for k, v in self.positions.items() if v.trade_id is not None
                          and self.store.get_trade(v.trade_id) and self.store.get_trade(v.trade_id).is_open}
        self._write_heartbeat()
        return orders

    def quotes_for(self, symbols: list[str]) -> dict:
        return self.broker.quotes(symbols)

    def _safety_net(self, now: datetime) -> int:
        orders = 0
        try:
            others: dict[str, float] = {}
            for t in self.store.open_trades(None):
                if t.sleeve_id != self.sleeve_id:
                    others[t.symbol] = others.get(t.symbol, 0.0) + t.signed_qty
            remaining = [p for p in self.broker.positions() if abs(p["qty"]) > 0]
            if not others:
                self.broker.close_all_positions()
                excess = [(p, p["qty"]) for p in remaining]
            else:
                excess = []
                for p in remaining:
                    ex = p["qty"] - others.get(p["symbol"], 0.0)
                    if abs(ex) > max(QTY_TOL, 0.01 * abs(p["qty"]) if is_crypto(p["symbol"]) else QTY_TOL):
                        side = "sell" if ex > 0 else "buy"
                        qty = abs(ex) if is_crypto(p["symbol"]) else int(round(abs(ex)))
                        try:
                            self.broker.submit_market(p["symbol"], qty, side)
                        except BrokerError as e:
                            self._log_error("flatten.safety_net", e)
                            continue
                        excess.append((p, ex))
            for p, qty in excess:
                sym = p["symbol"]
                px = p.get("current_price") or 0.0
                pos = self.positions.get(sym)
                if pos is not None:
                    self._record_close(pos, "eod_safety_net", px or pos.entry_price, None, None, None, now)
                else:
                    # a store trade whose close_position failed above, or an orphan
                    tr = next((t for t in self.store.open_trades(self.sleeve_id) if t.symbol == sym), None)
                    if tr is not None:
                        gross, cost, net = self.trade_economics(tr.side, tr.qty, tr.entry_price, px or tr.entry_price,
                                                                tr.entry_bid, tr.entry_ask, None, None)
                        self.store.close_trade(tr.id, now, px or tr.entry_price, None, None, None, "eod_safety_net",
                                               tr.bars_held, tr.mae_pct, tr.mfe_pct, gross, cost, net)
                        self.counters["closed_today"] += 1
                self.store.log_error("flatten", f"safety net closed {sym} qty {qty} (eod_safety_net)", None, now)
                orders += 1
        except Exception as e:
            self._log_error("flatten.safety_net", e)
        return orders


# ==========================================================================
# S0 — the Phase 0 VWAP reversion engine
# ==========================================================================

class Engine(SleeveBase):
    sleeve_id = "S0"

    def __init__(self, store: Store, broker, clock: MarketClock, universe: list[str], config_params: dict,
                 sleep_fn: Callable[[float], None] = time.sleep, sweep_fn: Optional[Callable] = None):
        self._init_base(store, broker, clock, sleep_fn)
        self.universe = list(universe)
        self.symbols = self.universe
        self.config_params = Params.from_dict(config_params)
        self.params: Params = self.config_params
        self.data = DataState(self.universe)
        self._sweep_fn = sweep_fn
        # per-day bookkeeping
        self._data_day: Optional[date] = None
        self._preopen_done: Optional[date] = None
        self._flattened: Optional[date] = None
        self._swept: Optional[date] = None
        self._last_cycle_mark: Optional[datetime] = None

    # ---- params ------------------------------------------------------------
    def load_params(self) -> Params:
        """Live params from the store; seeded from config.yaml the first time."""
        row = self.store.read_params(self.sleeve_id)
        if row is None:
            self.store.write_params(self.config_params.to_dict(), "config", self.now(), self.sleeve_id)
            self.store.write_params_history(self.now().date(), self.config_params.to_dict(), "config", None,
                                            self.sleeve_id)
            self.params = self.config_params
        else:
            try:
                p = Params.from_dict(row["params"])
                p.validate()
                self.params = p
            except (ValueError, TypeError) as e:
                self._log_error("params", e)
        return self.params

    # ---- startup / preopen -------------------------------------------------
    def startup(self, reconcile: bool = True) -> None:
        now = self.now()
        self.store.write_heartbeat(self.sleeve_id, state="idle", pid=os.getpid(), started_at=now)
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
        if self.twin is not None:
            self.twin.restore(now)
        if reconcile:
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
            for sym, bars in self.store.bars_for_day(session.date, symbols=self.universe).items():
                self.data.update(sym, bars)

    def _rebuild_cooldowns(self, session: Session) -> None:
        """After a restart: recover each symbol's bar count at its last exit today from closed trades.
        An exit at cycle hh:mm:05 acted on the bars before hh:mm:00."""
        for t in self.store.closed_trades_for_day(session.date, self.sleeve_id):
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

    # ---- hooks for SleeveBase ------------------------------------------------
    def last_price(self, symbol: str) -> Optional[float]:
        st = self.data.states.get(symbol)
        return st.last_close if st and st.last_close else None

    def on_exit_recorded(self, pos: Position) -> None:
        st = self.data.states.get(pos.symbol)
        self.exit_bar_count[pos.symbol] = st.bar_count if st else 0

    def quotes_for(self, symbols: list[str]) -> dict:
        return self.broker.quotes(self.universe)

    def adopt(self, t: Trade) -> Position:
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

    _adopt = adopt   # Phase 0 name

    # ---- orders ------------------------------------------------------------
    def open_position(self, sig: Signal, quotes: dict, now: datetime, qty: Optional[int] = None) -> Optional[Position]:
        """Returns None when the settled entry order filled nothing."""
        sym = sig.symbol
        qty = qty or position_qty(sig.price, self.params)
        q = quotes.get(sym) or {}
        got = self.enter(sym, sig.side, qty, sig.price, now)
        if got is None:
            return None
        qty, entry_price, oid = got
        news_day, event = self.news(now.date())
        tid = self.store.open_trade(sym, sig.side, qty, now, entry_price, oid, q.get("bid"), q.get("ask"),
                                    hypothesis(sig, self.params), self.params.to_dict(), self.sleeve_id,
                                    news_day=news_day, event=event)
        pos = Position(tid, sym, sig.side, qty, entry_price, now, self.data[sym].last_ts,
                       entry_bid=q.get("bid"), entry_ask=q.get("ask"))
        self.positions[sym] = pos
        self.counters["opened_today"] += 1
        log.info("OPEN S0 %s %s x%d @ %.4f (%s)", sig.side, sym, qty, entry_price, hypothesis(sig, self.params))
        return pos

    # ---- the cycle ---------------------------------------------------------
    def run_cycle(self, now: Optional[datetime] = None) -> CycleResult:
        """One full cycle. Never raises: every step is caught, logged and counted."""
        t0 = time.monotonic()
        now = now or self.now()
        res = CycleResult(ts=now, state=self.state, sleeve_id=self.sleeve_id)
        session = self.clock.today_session()
        paused = False
        self._last_cycle_mark = now.replace(second=CYCLE_OFFSET_S, microsecond=0)

        # 1. params + controls
        try:
            self.load_params()
            controls = self.store.read_controls()
            paused = controls["paused"]
            if controls["flatten_requested"] and self.handle_flatten_requests:
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
                    qty = None
                    if sig.fired:
                        qty = position_qty(sig.price, self.params)
                        gate = self.entry_gate(sym, sig.side, qty * sig.price, self.params.skip_news_days, now)
                        if gate:
                            sig.fired, sig.reason = False, gate
                    res.symbols_evaluated += 1
                    res.reasons[sig.reason] = res.reasons.get(sig.reason, 0) + 1
                    row = sig.to_row()
                    row["ts"] = now
                    row["params"] = params_dict
                    rows.append(row)
                    if sig.fired:
                        res.signals_fired += 1
                        self.open_position(sig, quotes, now, qty)
                        res.orders_placed += 1
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.entry", e)

            # 5b. random twin (simulated only)
            if self.twin is not None:
                try:
                    self.twin.on_cycle_s0(self, now, session, entries_ok)
                except Exception as e:
                    res.errors += 1
                    self._log_error("cycle.twin", e)

        # 6. cycle row + signals + heartbeat
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

    # ---- sweep -------------------------------------------------------------
    def run_sweep(self, session: Session) -> Optional[dict]:
        from .sweep import run_sweep
        fn = self._sweep_fn or run_sweep
        try:
            if self._sweep_fn is None:
                sessions = {}
                dates = self.store.bar_dates(5, symbols=self.universe)
                if dates:
                    d0 = date.fromisoformat(dates[0])
                    sessions = self.clock.sessions_between(d0 - timedelta(days=1), session.date)
                summary = fn(self.store, self.params, session.date, self.now(), sessions=sessions,
                             symbols=self.universe)
            else:
                summary = fn(self.store, self.params, session.date, self.now())
            log.info("sweep done: %s", summary.get("reason") if isinstance(summary, dict) else summary)
            return summary
        except Exception as e:
            self._log_error("sweep", e)
            return None

    # ---- state machine -----------------------------------------------------
    def _wait_until(self, target: datetime, heartbeat_every: float = 30.0) -> None:
        """Sleep in chunks until target, refreshing the heartbeat state (not last_cycle_ts)."""
        while not self._stop:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, heartbeat_every))
            self.store.write_heartbeat(self.sleeve_id, state=self.state)

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
        """One step of the state machine: plan, wait, act. Returns after at most ~1 minute of waiting."""
        when, action = self.plan(self.now())
        self._wait_until(when)
        if action is not None and not self._stop:
            action()

    def plan(self, now: datetime) -> tuple[datetime, Optional[Callable[[], object]]]:
        """Do whatever is due right now that is not a cycle (preopen, flatten, sweep) and return
        (when, action): the next time this sleeve needs the loop, and what to run then (a cycle) or None."""
        session = self.clock.today_session()
        if session is None or now >= session.sweep_at and self._swept == session.date:
            self.set_state("idle")
            nxt = self.clock.next_session()
            target = nxt.preopen_at if nxt else now + timedelta(hours=1)
            return min(target, now + timedelta(minutes=10)), None
        if now < session.preopen_at:
            self.set_state("idle")
            return session.preopen_at, None
        if now < session.flatten_at:
            if self._preopen_done != session.date:
                self.preopen(session)
            if now < session.open:
                self.set_state("preopen")
                return session.open, None
            self.set_state("running")
            # a minute whose cycle is overdue because another sleeve's work ran long: run it now,
            # provided the previous minute ran (a process that was simply not running does not catch up)
            mark = now.replace(second=CYCLE_OFFSET_S, microsecond=0)
            if (self._last_cycle_mark is not None and mark <= now and session.open <= mark < session.flatten_at
                    and mark - self._last_cycle_mark == timedelta(minutes=1)
                    and (now - mark).total_seconds() < LATE_CYCLE_S):
                return now, self.run_cycle
            target = self.next_cycle_time(now)
            if target >= session.flatten_at:
                return session.flatten_at, None
            return target, self.run_cycle
        if self._flattened != session.date:
            self.set_state("flattening")
            self.flatten("eod_flatten")
            if self.twin is not None:
                self.twin.eod(self, now)
            self._flattened = session.date
            self.set_state("postclose")
            return now, None
        if now < session.sweep_at:
            self.set_state("postclose")
            return session.sweep_at, None
        if self._swept != session.date:
            # `_swept` is lost on restart; the params_history row is the durable record that the
            # sweep ran, so a restart after sweep_at must not sweep (and step params) a second time.
            if self.store.has_sweep_for(session.date, self.sleeve_id):
                log.info("sweep for %s already in params_history; skipping", session.date)
            else:
                self.set_state("sweeping")
                self.run_sweep(session)
            self._swept = session.date
            self.set_state("idle")
            return now, None
        return now + timedelta(minutes=1), None   # unreachable; keeps the loop alive
