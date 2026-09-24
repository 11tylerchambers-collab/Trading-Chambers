"""Broker wrapper tests with the alpaca SDK clients replaced by fakes. No network."""
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chambers.broker import Broker, BrokerError
from chambers.clock import ET
from chambers.store import Store

UTC = ZoneInfo("UTC")


class Boom(Exception):
    pass


class FakeTrading:
    def __init__(self):
        self.submitted = []
        self.cancelled = False

    def get_account(self):
        return SimpleNamespace(portfolio_value="100000.5", buying_power="200000", cash="50000", equity="100000.5")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="AAPL", qty="10", side="PositionSide.LONG", avg_entry_price="100",
                                current_price="101", unrealized_pl="10"),
                SimpleNamespace(symbol="TSLA", qty="-5", side="PositionSide.SHORT", avg_entry_price="200",
                                current_price="190", unrealized_pl="50")]

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="ord-1")

    def get_order_by_id(self, oid):
        return SimpleNamespace(id=oid, symbol="AAPL", side=SimpleNamespace(value="buy"), qty="10",
                               status=SimpleNamespace(value="filled"), filled_qty="10", filled_avg_price="100.25",
                               filled_at=datetime(2026, 9, 22, 14, 0, 5, tzinfo=UTC))

    def get_orders(self, req):
        return []

    def cancel_orders(self):
        self.cancelled = True

    def cancel_order_by_id(self, oid):
        self.cancelled_ids = getattr(self, "cancelled_ids", []) + [oid]

    def get_clock(self):
        return SimpleNamespace(timestamp=datetime(2026, 9, 22, 14, 0, tzinfo=UTC), is_open=True,
                               next_open=datetime(2026, 9, 23, 13, 30, tzinfo=UTC),
                               next_close=datetime(2026, 9, 22, 20, 0, tzinfo=UTC))

    def get_calendar(self, req):
        return [SimpleNamespace(date=date(2026, 9, 22), open=datetime(2026, 9, 22, 9, 30),
                                close=datetime(2026, 9, 22, 16, 0))]


class FakeData:
    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        if self.fail:
            raise Boom("rate limited")
        bar = SimpleNamespace(timestamp=datetime(2026, 9, 22, 13, 30, tzinfo=UTC), open=1.0, high=2.0, low=0.5,
                              close=1.5, volume=100.0)
        return SimpleNamespace(data={"AAPL": [bar], "MSFT": []})

    def get_stock_latest_quote(self, req):
        self.requests.append(req)
        return {"SPY": SimpleNamespace(bid_price=500.0, ask_price=500.02, bid_size=1, ask_size=2,
                                       timestamp=datetime(2026, 9, 22, 14, 0, tzinfo=UTC))}


def make(tmp_path, fail_data=False):
    st = Store(tmp_path / "t.db")
    b = Broker("k", "s", paper=True, store=st)
    b._trading = FakeTrading()
    b._data = FakeData(fail=fail_data)
    return b, st


def test_refuses_live():
    with pytest.raises(BrokerError):
        Broker("k", "s", paper=False)


def test_account_positions_signed(tmp_path):
    b, _ = make(tmp_path)
    assert b.account() == {"portfolio_value": 100000.5, "buying_power": 200000.0, "cash": 50000.0, "equity": 100000.5}
    pos = b.positions()
    assert pos[0]["qty"] == 10 and pos[1]["qty"] == -5 and pos[1]["symbol"] == "TSLA"


def test_orders(tmp_path):
    b, _ = make(tmp_path)
    oid = b.submit_market("AAPL", 10, "buy")
    assert oid == "ord-1"
    req = b._trading.submitted[0]
    assert req.symbol == "AAPL" and req.qty == 10 and req.side.value == "buy" and req.time_in_force.value == "day"
    st = b.order_status(oid)
    assert st["status"] == "filled" and st["filled_avg_price"] == 100.25 and st["filled_qty"] == 10
    assert st["filled_at"].tzinfo is not None and st["filled_at"].hour == 10  # converted to ET
    b.cancel_all()
    assert b._trading.cancelled
    b.cancel_order("ord-1")
    assert b._trading.cancelled_ids == ["ord-1"]
    assert b.open_orders() == []


def test_bars_multi_symbol_single_request_et_conversion(tmp_path):
    b, _ = make(tmp_path)
    out = b.bars_1m(["AAPL", "MSFT", "NVDA"], datetime(2026, 9, 22, 9, 30, tzinfo=ET),
                    datetime(2026, 9, 22, 10, 0, tzinfo=ET))
    assert len(b._data.requests) == 1
    assert b._data.requests[0].feed.value == "iex"
    assert list(out) == ["AAPL"]  # empty symbols omitted, missing symbols omitted
    bar = out["AAPL"][0]
    assert bar.ts == datetime(2026, 9, 22, 9, 30, tzinfo=ET) and bar.c == 1.5 and bar.v == 100


def test_quotes(tmp_path):
    b, _ = make(tmp_path)
    q = b.quotes(["SPY", "QQQ"])
    assert len(b._data.requests) == 1
    assert q["SPY"]["bid"] == 500.0 and q["SPY"]["ask"] == 500.02 and q["SPY"]["ts"].tzinfo is not None
    assert "QQQ" not in q


def test_clock_and_calendar(tmp_path):
    b, _ = make(tmp_path)
    c = b.clock()
    assert c["is_open"] and c["timestamp"] == datetime(2026, 9, 22, 10, 0, tzinfo=ET)
    cal = b.calendar(date(2026, 9, 20), date(2026, 9, 25))
    assert cal[0]["open"] == datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    assert cal[0]["close"] == datetime(2026, 9, 22, 16, 0, tzinfo=ET)


def test_error_logged_and_wrapped(tmp_path):
    b, st = make(tmp_path, fail_data=True)
    with pytest.raises(BrokerError) as ei:
        b.bars_1m(["AAPL"], datetime(2026, 9, 22, 9, 30, tzinfo=ET), datetime(2026, 9, 22, 10, 0, tzinfo=ET))
    assert "rate limited" in str(ei.value)
    errs = st.recent_errors(5)
    assert len(errs) == 1 and errs[0]["where_"] == "broker.bars_1m" and "Boom" in errs[0]["message"]
    assert "Traceback" in errs[0]["traceback"]
