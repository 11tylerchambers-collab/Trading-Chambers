"""Shared test doubles: a mock broker with instant fills and a controllable clock."""
from datetime import date, datetime, timedelta

from chambers.broker import BrokerError
from chambers.clock import ET, MarketClock
from chambers.store import Bar

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "JPM",
            "BAC", "XOM", "SPY", "QQQ", "COST", "WMT", "DIS", "INTC", "CRM", "AVGO"]


class FakeTime:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class MockBroker:
    """In-memory broker. Market orders fill instantly at `prices[symbol]` (or the last bar close)."""

    def __init__(self, sessions=None, bars=None, prices=None, fail=None):
        # sessions: list of (date, open_dt, close_dt); default one regular session on 2026-09-22
        d = date(2026, 9, 22)
        self.sessions = sessions or [(d, datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                                      datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))]
        self.bars = bars or {}            # symbol -> list[Bar]
        self.prices = prices or {}        # symbol -> fill price
        self.fail = set(fail or [])       # method names that raise BrokerError
        self.positions_ = {}              # symbol -> signed qty
        self.avg_entry = {}
        self.orders = {}                  # id -> dict
        self.submitted = []
        self.cancel_all_calls = 0
        self.close_all_calls = 0
        self.calls = {}
        self.fill_status = "filled"
        self.partial_qty = 0              # with fill_status "partially_filled": shares filled at once
        self.fill_after_polls = None      # with a non-filled fill_status: the order fills in full on this poll
        self.cancelled_orders = []

    def _hit(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.fail:
            raise BrokerError(f"{name}: mock failure")

    def _price(self, sym):
        if sym in self.prices:
            return self.prices[sym]
        bs = self.bars.get(sym)
        return bs[-1].c if bs else 100.0

    def account(self):
        self._hit("account")
        return {"portfolio_value": 100000.0, "buying_power": 200000.0, "cash": 100000.0, "equity": 100000.0}

    def positions(self):
        self._hit("positions")
        out = []
        for sym, qty in self.positions_.items():
            if qty:
                out.append({"symbol": sym, "qty": qty, "avg_entry_price": self.avg_entry.get(sym, self._price(sym)),
                            "current_price": self._price(sym), "unrealized_pl": 0.0})
        return out

    def open_orders(self):
        self._hit("open_orders")
        return []

    def submit_market(self, symbol, qty, side):
        self._hit("submit_market")
        oid = f"o{len(self.orders) + 1}"
        px = self._price(symbol)
        signed = qty if side == "buy" else -qty
        self.submitted.append({"id": oid, "symbol": symbol, "qty": qty, "side": side, "price": px})
        filled = qty if self.fill_status == "filled" else (self.partial_qty if self.fill_status == "partially_filled" else 0)
        self.orders[oid] = {"status": self.fill_status, "filled_qty": float(filled),
                            "filled_avg_price": px if filled else None,
                            "filled_at": datetime(2026, 9, 22, 10, 0, tzinfo=ET),
                            "_symbol": symbol, "_qty": qty, "_sign": 1 if side == "buy" else -1, "_px": px, "_polls": 0}
        self._move(symbol, signed / qty * filled if filled else 0, px)
        return oid

    def _move(self, symbol, signed, px):
        if not signed:
            return
        new = self.positions_.get(symbol, 0) + int(signed)
        if new == 0:
            self.positions_.pop(symbol, None)
        else:
            self.positions_[symbol] = new
            self.avg_entry[symbol] = px

    def order_status(self, order_id):
        self._hit("order_status")
        o = self.orders[order_id]
        o["_polls"] += 1
        if self.fill_after_polls is not None and o["status"] not in ("filled", "canceled") \
                and o["_polls"] >= self.fill_after_polls:
            rest = o["_qty"] - int(o["filled_qty"])
            self._move(o["_symbol"], o["_sign"] * rest, o["_px"])
            o.update(status="filled", filled_qty=float(o["_qty"]), filled_avg_price=o["_px"])
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def cancel_order(self, order_id):
        self._hit("cancel_order")
        self.cancelled_orders.append(order_id)
        o = self.orders[order_id]
        if o["status"] != "filled":
            o["status"] = "canceled"

    def cancel_all(self):
        self._hit("cancel_all")
        self.cancel_all_calls += 1

    def close_all_positions(self):
        self._hit("close_all_positions")
        self.close_all_calls += 1
        out = [{"symbol": s, "order_id": f"safety-{s}", "status": 200} for s in self.positions_]
        self.positions_ = {}
        return out

    def bars_1m(self, symbols, start, end):
        self._hit("bars_1m")
        out = {}
        for s in symbols:
            sel = [b for b in self.bars.get(s, []) if start <= b.ts <= end]
            if sel:
                out[s] = sel
        return out

    def quotes(self, symbols):
        self._hit("quotes")
        out = {}
        for s in symbols:
            px = self._price(s)
            out[s] = {"bid": px - 0.01, "ask": px + 0.01, "bid_size": 1, "ask_size": 1,
                      "ts": datetime(2026, 9, 22, 10, 0, tzinfo=ET)}
        return out

    def clock(self):
        self._hit("clock")
        return {}

    def calendar(self, start, end):
        self._hit("calendar")
        return [{"date": d, "open": o, "close": c} for d, o, c in self.sessions if start <= d <= end]


def flat_bars(symbol_close, start, n, vol=100.0):
    """n flat bars (h=l=c) from start."""
    return [Bar(start + timedelta(minutes=i), symbol_close, symbol_close, symbol_close, symbol_close, vol)
            for i in range(n)]


def make_clock(ft: FakeTime, broker: MockBroker) -> MarketClock:
    return MarketClock(broker, now_fn=ft)
