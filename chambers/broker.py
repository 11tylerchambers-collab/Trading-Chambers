"""Thin wrapper over alpaca-py.

Every public method catches SDK/network exceptions, logs them to
`store.errors` (where_ = "broker.<method>"), and re-raises `BrokerError`.
Multi-symbol endpoints are always used for bars and quotes: one request
per cycle each, never a per-symbol loop over the network.
"""
from __future__ import annotations

import logging
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Optional

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from .clock import ET, to_et
from .store import Bar, Store

log = logging.getLogger("chambers.broker")


class BrokerError(Exception):
    """Single exception type raised by every Broker method on failure."""


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Broker:
    def __init__(self, api_key: str, secret_key: str, paper: bool, store: Optional[Store] = None):
        if not paper:
            raise BrokerError("Phase 0 never trades a live account: paper must be True")
        self._trading = TradingClient(api_key, secret_key, paper=True)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._store = store
        self.paper = True

    # ---- error handling ----------------------------------------------------
    def _fail(self, where: str, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        log.warning("broker.%s failed: %s", where, msg)
        if self._store is not None:
            try:
                self._store.log_error(f"broker.{where}", msg, traceback.format_exc())
            except Exception:  # never let logging kill the caller
                log.exception("could not write broker error to store")
        raise BrokerError(f"{where}: {msg}") from exc

    # ---- account / positions / orders -------------------------------------
    def account(self) -> dict:
        try:
            a = self._trading.get_account()
            return {"portfolio_value": _f(a.portfolio_value), "buying_power": _f(a.buying_power),
                    "cash": _f(a.cash), "equity": _f(a.equity)}
        except Exception as e:
            self._fail("account", e)

    def positions(self) -> list[dict]:
        try:
            out = []
            for p in self._trading.get_all_positions():
                qty = abs(_f(p.qty) or 0.0)
                if str(p.side).lower().endswith("short"):
                    qty = -qty
                out.append({"symbol": p.symbol, "qty": qty, "avg_entry_price": _f(p.avg_entry_price),
                            "current_price": _f(p.current_price), "unrealized_pl": _f(p.unrealized_pl)})
            return out
        except Exception as e:
            self._fail("positions", e)

    def open_orders(self) -> list[dict]:
        try:
            orders = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            return [self._order_dict(o) for o in orders]
        except Exception as e:
            self._fail("open_orders", e)

    @staticmethod
    def _order_dict(o) -> dict:
        return {"id": str(o.id), "symbol": o.symbol, "side": str(o.side.value) if o.side else None,
                "qty": _f(o.qty), "status": str(o.status.value), "filled_qty": _f(o.filled_qty) or 0.0,
                "filled_avg_price": _f(o.filled_avg_price),
                "filled_at": to_et(o.filled_at) if o.filled_at else None}

    def submit_market(self, symbol: str, qty: int, side: str) -> str:
        """side: 'buy' | 'sell'. Time in force: day. Returns the order id."""
        try:
            req = MarketOrderRequest(symbol=symbol, qty=int(qty),
                                     side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY)
            o = self._trading.submit_order(req)
            return str(o.id)
        except Exception as e:
            self._fail("submit_market", e)

    def order_status(self, order_id: str) -> dict:
        try:
            return self._order_dict(self._trading.get_order_by_id(order_id))
        except Exception as e:
            self._fail("order_status", e)

    def cancel_all(self) -> None:
        try:
            self._trading.cancel_orders()
        except Exception as e:
            self._fail("cancel_all", e)

    def close_all_positions(self) -> list[dict]:
        """Safety net used by flatten. Returns [{symbol, order_id, status}]."""
        try:
            resp = self._trading.close_all_positions(cancel_orders=True)
            out = []
            for r in resp or []:
                body = getattr(r, "body", None)
                out.append({"symbol": getattr(r, "symbol", None),
                            "order_id": str(getattr(body, "id", "")) if body is not None else None,
                            "status": getattr(r, "status", None)})
            return out
        except Exception as e:
            self._fail("close_all_positions", e)

    # ---- market data -------------------------------------------------------
    def bars_1m(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, list[Bar]]:
        """One multi-symbol request on the IEX feed. Bars converted to ET. Symbols with no bars are
        logged at debug level and omitted from the result."""
        if not symbols:
            return {}
        try:
            req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
                                   start=start, end=end, feed=DataFeed.IEX)
            resp = self._data.get_stock_bars(req)
            data = getattr(resp, "data", None) or {}
            out: dict[str, list[Bar]] = {}
            for sym in symbols:
                raw = data.get(sym) or []
                if not raw:
                    log.debug("no bars for %s in [%s, %s]", sym, start, end)
                    continue
                out[sym] = [Bar(to_et(b.timestamp), float(b.open), float(b.high), float(b.low),
                                float(b.close), float(b.volume)) for b in raw]
            return out
        except Exception as e:
            self._fail("bars_1m", e)

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """One multi-symbol latest-quote request on the IEX feed."""
        if not symbols:
            return {}
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX)
            resp = self._data.get_stock_latest_quote(req)
            out = {}
            for sym, q in (resp or {}).items():
                out[sym] = {"bid": _f(q.bid_price), "ask": _f(q.ask_price), "bid_size": _f(q.bid_size),
                            "ask_size": _f(q.ask_size), "ts": to_et(q.timestamp)}
            return out
        except Exception as e:
            self._fail("quotes", e)

    # ---- clock / calendar --------------------------------------------------
    def clock(self) -> dict:
        try:
            c = self._trading.get_clock()
            return {"timestamp": to_et(c.timestamp), "is_open": bool(c.is_open),
                    "next_open": to_et(c.next_open), "next_close": to_et(c.next_close)}
        except Exception as e:
            self._fail("clock", e)

    def calendar(self, start: date, end: date) -> list[dict]:
        """[{date, open, close}] with open/close as aware ET datetimes."""
        try:
            rows = self._trading.get_calendar(GetCalendarRequest(start=start, end=end))
            out = []
            for r in rows:
                o, c = r.open, r.close
                # alpaca-py builds naive datetimes from the calendar's date + time; they are ET wall times.
                o = o.replace(tzinfo=ET) if o.tzinfo is None else to_et(o)
                c = c.replace(tzinfo=ET) if c.tzinfo is None else to_et(c)
                out.append({"date": r.date, "open": o, "close": c})
            return out
        except Exception as e:
            self._fail("calendar", e)
