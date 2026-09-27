"""1% ATR sizing and caps; daily loss limit, same-side cap, exposure cap (PHASE1A §3). No network."""
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.engine import Engine
from chambers.risk import Portfolio, risk_qty
from chambers.store import Bar, Store
from chambers.strategy import Params

from .mocks import FakeTime, MockBroker, flat_bars, make_clock

D = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
CFG = Params().to_dict()


class Alerts:
    def __init__(self):
        self.sent = []

    def alert(self, key, msg, now=None):
        self.sent.append((key, msg))


def test_risk_qty_equities():
    # $20k × 1% = $200 risk; stop $2 → 100 shares; cap 50% = $10k at $50 → 200 shares: not binding
    assert risk_qty(20000, 0.01, 2.0, 50.0, 0.5) == (100, None)
    # cap binds: $10k / $400 = 25 shares
    assert risk_qty(20000, 0.01, 2.0, 400.0, 0.5) == (25, None)
    # floor, then min 1: $200 / $300 stop = 0.67 → 1 share (cap allows it)
    assert risk_qty(20000, 0.01, 300.0, 500.0, 0.5) == (1, None)
    # one share exceeds the cap → skipped
    assert risk_qty(20000, 0.01, 2.0, 12000.0, 0.5) == (0, "notional_cap")
    assert risk_qty(20000, 0.01, 0.0, 50.0, 0.5) == (0, "no_stop")
    assert risk_qty(20000, 0.01, None, 50.0, 0.5) == (0, "no_stop")


def test_risk_qty_crypto_fractional():
    q, why = risk_qty(20000, 0.01, 1500.0, 100000.0, 0.5, fractional=True)
    assert why is None and q == pytest.approx(0.1)                  # $10k cap binds: 0.1 BTC (risk would give 0.133)
    q, why = risk_qty(20000, 0.01, 4000.0, 100000.0, 0.5, fractional=True)
    assert q == pytest.approx(0.05) and why is None                 # $200 / $4000 = 0.05 BTC
    assert risk_qty(20, 0.01, 4000.0, 100000.0, 0.5, fractional=True) == (0, "notional_cap")   # below 0.0001


def make(tmp_path, now, equity=100000.0):
    ft = FakeTime(now)
    broker = MockBroker()
    broker.equity = equity
    st = Store(tmp_path / "t.db")
    clk = make_clock(ft, broker)
    alerts = Alerts()
    mono = {"t": 0.0}
    pf = Portfolio(st, broker, clk, daily_loss_pct=0.02, max_same_side=3, notifier=alerts,
                   monotonic=lambda: mono["t"])
    return pf, st, broker, ft, alerts, mono


class Sleeve:
    def __init__(self, equity_sleeve=True):
        self.equity_sleeve = equity_sleeve


def test_daily_loss_limit_trips_once_and_persists(tmp_path):
    pf, st, broker, ft, alerts, mono = make(tmp_path, OPEN + timedelta(minutes=1))
    assert pf.check_daily_loss(ft.now) is False
    assert st.risk_day(D)["open_equity"] == 100000.0
    broker.equity = 98100.0            # -1.9%: under the limit
    mono["t"] += 31
    assert pf.check_daily_loss(ft.now) is False
    broker.equity = 97900.0            # -2.1%
    mono["t"] += 31
    assert pf.check_daily_loss(ft.now) is True
    assert pf.allow_entry(Sleeve(), "AAPL", "long", 1000, ft.now) == "daily_loss_halt"
    assert pf.allow_entry(Sleeve(equity_sleeve=False), "BTC/USD", "long", 1000, ft.now) is None   # S2 is not halted
    broker.equity = 99000.0            # recovers: still halted for the session
    mono["t"] += 31
    assert pf.check_daily_loss(ft.now) is True
    assert [k for k, _ in alerts.sent] == ["daily_loss"]
    assert st.errors_count(where="risk.daily_loss") == 1
    # a new Portfolio (restart) reads the halt back
    pf2 = Portfolio(st, broker, pf.clock, notifier=alerts)
    assert pf2.check_daily_loss(ft.now) is True
    # outside the session nothing is halted
    assert pf.check_daily_loss(datetime(2026, 9, 22, 16, 30, tzinfo=ET)) is False


def test_same_side_and_exposure_caps(tmp_path):
    pf, st, broker, ft, alerts, mono = make(tmp_path, OPEN + timedelta(minutes=1), equity=10000.0)
    for i, sym in enumerate(["AAPL", "MSFT", "NVDA"]):
        st.open_trade(sym, "long", 10, ft.now, 100.0, f"o{i}", None, None, {}, {}, sleeve_id=["S0", "S1", "S3"][i])
    assert pf.allow_entry(Sleeve(), "AMD", "long", 100, ft.now) == "same_side_cap"     # 3 longs across sleeves
    assert pf.allow_entry(Sleeve(), "AMD", "short", 100, ft.now) is None
    # gross 3 × $1000 = $3000; equity $10000 → a $7001 entry breaks "no leverage"
    assert pf.allow_entry(Sleeve(), "AMD", "short", 7001, ft.now) == "exposure_cap"
    assert pf.allow_entry(Sleeve(), "AMD", "short", 7000, ft.now) is None


def test_s0_logs_gate_reason_and_does_not_trade(tmp_path):
    bars = flat_bars(100.0, OPEN, 30) + [Bar(OPEN + timedelta(minutes=30), 99, 99, 99, 99, 500.0)]
    ft = FakeTime(OPEN + timedelta(minutes=31, seconds=5))
    broker = MockBroker(bars={"AAPL": bars})
    st = Store(tmp_path / "t.db")
    clk = make_clock(ft, broker)
    eng = Engine(st, broker, clk, ["AAPL"], CFG, sleep_fn=ft.sleep)
    eng.portfolio = Portfolio(st, broker, clk, max_same_side=0)
    eng.startup()
    res = eng.run_cycle()
    assert res.reasons == {"same_side_cap": 1} and res.signals_fired == 0 and broker.submitted == []
    sig = st.signals_for_day(D)[-1]
    assert sig["fired"] == 0 and sig["reason"] == "same_side_cap" and sig["dev_pct"] < -0.3


def test_s0_news_day_skip(tmp_path):
    bars = flat_bars(100.0, OPEN, 30) + [Bar(OPEN + timedelta(minutes=30), 99, 99, 99, 99, 500.0)]
    ft = FakeTime(OPEN + timedelta(minutes=31, seconds=5))
    broker = MockBroker(bars={"AAPL": bars})
    st = Store(tmp_path / "t.db")
    st.replace_econ_events([{"date": "2026-09-22", "event": "CPI (Aug)", "time": "08:30"}])
    eng = Engine(st, broker, make_clock(ft, broker), ["AAPL"], {**CFG, "skip_news_days": True}, sleep_fn=ft.sleep)
    eng.startup()
    assert eng.run_cycle().reasons == {"news_day": 1} and broker.submitted == []
    st.write_params({**CFG, "skip_news_days": False}, "manual", ft.now)
    ft.advance(minutes=1)
    eng.run_cycle()
    t = st.open_trades()[0]
    assert t.news_day == 1 and t.event == "CPI (Aug)"
