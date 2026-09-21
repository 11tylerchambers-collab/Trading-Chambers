"""Engine cycle, flatten, reconcile and state-machine tests with the mock broker. No network."""
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.engine import Engine
from chambers.store import Bar, Store
from chambers.strategy import Params

from .mocks import UNIVERSE, FakeTime, MockBroker, flat_bars, make_clock

D = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
CFG = Params().to_dict()


def make_engine(tmp_path, now, broker=None, universe=UNIVERSE, params=CFG):
    ft = FakeTime(now)
    broker = broker or MockBroker()
    st = Store(tmp_path / "t.db")
    clk = make_clock(ft, broker)
    eng = Engine(st, broker, clk, universe, params, sleep_fn=ft.sleep)
    return eng, st, broker, ft


# ---------------------------------------------------------------- step 7: --once after hours

def test_once_after_hours_writes_cycle_and_20_entries_closed_signals(tmp_path):
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 18, 0, 5, tzinfo=ET))
    eng.startup()
    before = dict(broker.calls)
    res = eng.run_cycle()
    assert res.symbols_evaluated == 20 and res.errors == 0 and res.cycle_id is not None
    cycles = st.cycles_for_day(D)
    assert len(cycles) == 1 and cycles[0]["symbols_evaluated"] == 20
    sigs = st.signals_for_day(D)
    assert len(sigs) == 20 and all(s["reason"] == "entries_closed" for s in sigs)
    assert all(s["cycle_id"] == res.cycle_id for s in sigs)
    # one multi-symbol bars call and one quotes call per cycle, nothing per symbol
    assert broker.calls["bars_1m"] - before.get("bars_1m", 0) == 1
    assert broker.calls["quotes"] - before.get("quotes", 0) == 1
    hb = st.read_heartbeat()
    assert hb["cycles_today"] == 1 and hb["signals_today"] == 20 and hb["last_cycle_ts"] is not None
    assert st.read_params()["source"] == "config"  # params seeded from config on first run


def test_once_on_non_session_day_still_logs(tmp_path):
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 26, 12, 0, 5, tzinfo=ET))  # Saturday
    eng.startup()
    res = eng.run_cycle()
    assert res.symbols_evaluated == 20 and res.errors == 0
    assert all(s["reason"] == "entries_closed" for s in st.signals_for_day(date(2026, 9, 26)))
    assert "bars_1m" not in broker.calls  # nothing to fetch without a session


# ---------------------------------------------------------------- live-like cycles

def dip_day_bars(sym_price=100.0):
    """30 flat bars then a dip bar; the engine sees the dip on the cycle at 10:01:05."""
    bars = flat_bars(sym_price, OPEN, 30)
    d = sym_price * 0.99
    bars.append(Bar(OPEN + timedelta(minutes=30), d, d, d, d, 500.0))
    return bars


def test_entry_then_vwap_touch_exit(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()})
    for s in UNIVERSE[1:]:
        broker.bars[s] = flat_bars(100.0, OPEN, 31)
    now = OPEN + timedelta(minutes=31, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker)
    eng.startup()
    res = eng.run_cycle()
    assert res.signals_fired == 1 and res.orders_placed == 1 and res.errors == 0
    assert res.reasons == {"fired": 1, "dev_too_small": 19}
    ot = st.open_trades()
    assert len(ot) == 1
    t = ot[0]
    assert t.symbol == "AAPL" and t.side == "long" and t.qty == 20 and t.entry_price == 99.0
    assert t.entry_bid == pytest.approx(98.99) and t.entry_ask == pytest.approx(99.01)
    assert t.to_dict()["hypothesis"]["expect"] == "return to vwap" and t.entry_order_id == "o1"
    assert broker.submitted[0]["side"] == "buy" and broker.positions_ == {"AAPL": 20}
    assert "AAPL" in eng.positions and eng.positions["AAPL"].bars_held == 0

    # next minute: recovery bar above vwap → vwap_touch
    broker.bars["AAPL"].append(Bar(OPEN + timedelta(minutes=31), 100.5, 100.5, 100.5, 100.5, 100.0))
    broker.prices["AAPL"] = 100.5
    ft.advance(minutes=1)
    res2 = eng.run_cycle()
    assert res2.orders_placed == 1 and res2.errors == 0
    assert st.open_trades() == [] and broker.positions_ == {}
    closed = st.closed_trades_for_day(D)
    assert len(closed) == 1
    c = closed[0]
    assert c.exit_reason == "vwap_touch" and c.exit_price == 100.5 and c.bars_held == 1
    assert c.gross_pnl == pytest.approx(1.5 * 20)
    assert c.est_cost == pytest.approx((0.01 + 0.01) * 20 + 0.0001 * 99.0 * 20 * 2)
    assert c.net_pnl == pytest.approx(c.gross_pnl - c.est_cost)
    assert c.mfe_pct == pytest.approx((100.5 - 99) / 99 * 100) and c.mae_pct == 0.0
    assert c.hypothesis_json and c.exit_bid == pytest.approx(100.49)
    # the exit bar was also evaluated for a fresh entry (not fired: dev small) → 20 signals again
    assert len(st.signals_for_day(D)) == 40
    hb = st.read_heartbeat()
    assert hb["opened_today"] == 1 and hb["closed_today"] == 1 and hb["open_positions"] == 0
    assert hb["net_pnl_today"] == pytest.approx(c.net_pnl)


def test_paused_blocks_entries_but_exits_run(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()})
    now = OPEN + timedelta(minutes=31, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    eng.startup()
    st.set_paused(True, now)
    res = eng.run_cycle()
    assert res.signals_fired == 0 and res.reasons == {"entries_closed": 1}
    st.set_paused(False, now)
    res = eng.run_cycle()
    assert res.signals_fired == 1 and len(eng.positions) == 1
    st.set_paused(True, now)
    broker.bars["AAPL"].append(Bar(OPEN + timedelta(minutes=31), 100.5, 100.5, 100.5, 100.5, 100.0))
    ft.advance(minutes=1)
    res = eng.run_cycle()
    assert len(eng.positions) == 0 and st.closed_trades_for_day(D)[0].exit_reason == "vwap_touch"


def test_bars_failure_writes_cycle_with_error_and_no_signals(tmp_path):
    broker = MockBroker(fail=["bars_1m"])
    eng, st, broker, ft = make_engine(tmp_path, OPEN + timedelta(minutes=40, seconds=5), broker)
    eng.startup()  # seed_bars fails too, is logged, does not raise
    res = eng.run_cycle()
    assert res.errors >= 1 and res.symbols_evaluated == 0
    cycles = st.cycles_for_day(D)
    assert len(cycles) == 1 and cycles[0]["errors"] >= 1
    assert st.signals_for_day(D) == []
    assert st.errors_count(where="cycle.bars") == 1
    assert st.errors_count(where="unhandled") == 0


def test_strategy_exception_is_caught_per_symbol(tmp_path, monkeypatch):
    import chambers.engine as engmod
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 18, 0, 5, tzinfo=ET))
    eng.startup()
    calls = {"n": 0}
    real = engmod.evaluate

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("strategy bug")
        return real(*a, **k)

    monkeypatch.setattr(engmod, "evaluate", boom)
    res = eng.run_cycle()
    assert res.errors == 1 and res.symbols_evaluated == 19
    assert len(st.signals_for_day(D)) == 19
    assert st.errors_count(where="cycle.entry") == 1
    assert st.read_heartbeat()["last_error"].startswith("cycle.entry: RuntimeError")


def test_quotes_failure_is_non_fatal(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()}, fail=["quotes"])
    eng, st, broker, ft = make_engine(tmp_path, OPEN + timedelta(minutes=31, seconds=5), broker, universe=["AAPL"])
    eng.startup()
    res = eng.run_cycle()
    assert res.errors == 1 and res.signals_fired == 1
    t = st.open_trades()[0]
    assert t.entry_bid is None and t.entry_ask is None


def test_unfilled_order_is_recorded_at_last_close(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()})
    broker.fill_status = "accepted"
    eng, st, broker, ft = make_engine(tmp_path, OPEN + timedelta(minutes=31, seconds=5), broker, universe=["AAPL"])
    eng.startup()
    res = eng.run_cycle()
    assert res.signals_fired == 1
    t = st.open_trades()[0]
    assert t.entry_price == 99.0 and t.entry_order_id == "o1"
    assert st.errors_count(where="cycle.entry") == 1
    assert broker.calls["order_status"] >= 2  # polled until the 5s timeout


def test_params_reload_every_cycle_and_bad_params_ignored(tmp_path):
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 18, 0, 5, tzinfo=ET))
    eng.startup()
    st.write_params({**CFG, "entry_dev_pct": 0.6}, "manual", ft.now)
    eng.run_cycle()
    assert eng.params.entry_dev_pct == 0.6
    st.write_params({**CFG, "entry_dev_pct": 0}, "manual", ft.now)  # invalid → keep previous
    eng.run_cycle()
    assert eng.params.entry_dev_pct == 0.6 and st.errors_count(where="params") == 1


def test_seed_bars_on_startup_and_restart_from_store(tmp_path):
    broker = MockBroker(bars={"AAPL": flat_bars(100.0, OPEN, 40)})
    now = OPEN + timedelta(minutes=40, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    eng.startup()
    assert eng.data["AAPL"].bar_count == 40
    assert len(st.bars_for_day(D)["AAPL"]) == 40
    # a second engine on the same db rebuilds from the store and only fetches what's newer
    broker2 = MockBroker(bars={"AAPL": flat_bars(100.0, OPEN, 42)})
    eng2 = Engine(Store(tmp_path / "t.db"), broker2, make_clock(FakeTime(now + timedelta(minutes=2)), broker2),
                  ["AAPL"], CFG, sleep_fn=lambda s: None)
    eng2.startup()
    assert eng2.data["AAPL"].bar_count == 42
    fetched = [b for b in broker2.bars["AAPL"] if b.ts >= OPEN + timedelta(minutes=40)]
    assert len(fetched) == 2


# ---------------------------------------------------------------- flatten

def test_flatten_closes_trades_then_safety_net_then_cancel_all(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars(), "MSFT": dip_day_bars()})
    now = OPEN + timedelta(minutes=31, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL", "MSFT"])
    eng.startup()
    eng.run_cycle()
    assert len(eng.positions) == 2
    # an extra position the engine doesn't know about (e.g. a stuck fill) exists at the broker
    broker.positions_["TSLA"] = 7
    orders = eng.flatten("eod_flatten", now)
    assert eng.positions == {} and broker.positions_ == {}
    assert broker.close_all_calls == 1 and broker.cancel_all_calls == 1
    closed = st.closed_trades_for_day(D)
    assert sorted(t.exit_reason for t in closed) == ["eod_flatten", "eod_flatten"]
    errs = [e for e in st.recent_errors(10) if e["where_"] == "flatten"]
    assert len(errs) == 1 and "TSLA" in errs[0]["message"] and "eod_safety_net" in errs[0]["message"]
    assert orders == 3


def test_flatten_safety_net_closes_store_trade_when_exit_order_fails(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()})
    now = OPEN + timedelta(minutes=31, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    eng.startup()
    eng.run_cycle()
    broker.fail.add("submit_market")  # exit order fails → safety net must catch it
    eng.flatten("eod_flatten", now)
    t = st.closed_trades_for_day(D)[0]
    assert t.exit_reason == "eod_safety_net" and t.exit_price == 99.0 and t.net_pnl is not None
    assert eng.positions == {} and broker.positions_ == {}


def test_flatten_requested_control_runs_flatten_in_cycle(tmp_path):
    broker = MockBroker(bars={"AAPL": dip_day_bars()})
    now = OPEN + timedelta(minutes=31, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    eng.startup()
    eng.run_cycle()
    assert len(eng.positions) == 1
    st.request_flatten(now)
    st.set_paused(True, now)  # so it doesn't immediately re-enter on the same bar
    res = eng.run_cycle()
    assert eng.positions == {} and st.read_controls()["flatten_requested"] is False
    assert st.closed_trades_for_day(D)[0].exit_reason == "manual_flatten"
    assert res.orders_placed >= 1


# ---------------------------------------------------------------- step 11: reconcile

def test_reconcile_orphan_is_flattened_and_logged(tmp_path):
    broker = MockBroker()
    broker.positions_ = {"TSLA": 5, "NVDA": -3}
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 9, 20, tzinfo=ET), broker)
    eng.startup()
    assert broker.positions_ == {}
    sells = [o for o in broker.submitted if o["symbol"] == "TSLA"]
    buys = [o for o in broker.submitted if o["symbol"] == "NVDA"]
    assert sells[0]["side"] == "sell" and sells[0]["qty"] == 5
    assert buys[0]["side"] == "buy" and buys[0]["qty"] == 3
    errs = [e for e in st.recent_errors(10) if e["where_"] == "reconcile"]
    assert len(errs) == 2 and all("orphan" in e["message"] for e in errs)
    assert eng.positions == {}


def test_reconcile_missing_broker_position_closes_store_trade(tmp_path):
    broker = MockBroker(bars={"AAPL": flat_bars(101.0, OPEN, 40)})
    now = OPEN + timedelta(minutes=40, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    tid = st.open_trade("AAPL", "long", 20, now - timedelta(minutes=5), 100.0, "old", None, None, {"x": 1}, CFG)
    eng.startup()
    t = st.get_trade(tid)
    assert not t.is_open and t.exit_reason == "reconcile_missing"
    assert t.exit_price == 101.0  # last known close from the seeded bars
    assert t.gross_pnl == pytest.approx(20.0) and t.net_pnl is not None
    assert eng.positions == {}
    assert st.errors_count(where="reconcile") == 1


def test_reconcile_adopts_matching_position_with_rebuilt_excursions(tmp_path):
    bars = flat_bars(100.0, OPEN, 30) + [Bar(OPEN + timedelta(minutes=30 + i), c, c, c, c, 100.0)
                                          for i, c in enumerate([99.0, 98.7, 99.4])]
    broker = MockBroker(bars={"AAPL": bars})
    broker.positions_ = {"AAPL": 20}
    now = OPEN + timedelta(minutes=33, seconds=5)
    eng, st, broker, ft = make_engine(tmp_path, now, broker, universe=["AAPL"])
    # trade opened by a previous process on the cycle that saw bar 30 (10:01:05)
    st.open_trade("AAPL", "long", 20, OPEN + timedelta(minutes=31, seconds=5), 99.0, "old", 98.99, 99.01, {"x": 1}, CFG)
    eng.startup()
    assert "AAPL" in eng.positions and broker.submitted == []
    pos = eng.positions["AAPL"]
    assert pos.bars_held == 2  # bars at 10:01 and 10:02 came after the entry bar (10:00)
    assert pos.mae_pct == pytest.approx((98.7 - 99) / 99 * 100)
    assert pos.mfe_pct == pytest.approx((99.4 - 99) / 99 * 100)
    # sign mismatch → not adopted: store trade closed as missing, broker position flattened as orphan
    st2 = Store(tmp_path / "t2.db")
    broker2 = MockBroker(bars={"AAPL": bars})
    broker2.positions_ = {"AAPL": -20}
    eng2 = Engine(st2, broker2, make_clock(FakeTime(now), broker2), ["AAPL"], CFG, sleep_fn=lambda s: None)
    st2.open_trade("AAPL", "long", 20, now - timedelta(minutes=2), 99.0, "old", None, None, {"x": 1}, CFG)
    eng2.startup()
    assert eng2.positions == {} and broker2.positions_ == {}
    assert st2.get_trade(1).exit_reason == "reconcile_missing"


def test_reconcile_broker_failure_is_logged_not_raised(tmp_path):
    broker = MockBroker(fail=["positions"])
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 9, 20, tzinfo=ET), broker)
    eng.startup()
    assert st.errors_count(where="reconcile") == 1


# ---------------------------------------------------------------- state machine

def test_tick_walks_through_the_day(tmp_path):
    broker = MockBroker(bars={"AAPL": flat_bars(100.0, OPEN, 400)})
    ft = FakeTime(datetime(2026, 9, 22, 9, 0, tzinfo=ET))
    st = Store(tmp_path / "t.db")
    sweeps = []

    def fake_sweep(store, params, d, now):
        sweeps.append(d)
        return {"reason": "test"}

    eng = Engine(st, broker, make_clock(ft, broker), ["AAPL"], CFG, sleep_fn=ft.sleep, sweep_fn=fake_sweep)
    eng.startup()
    eng.tick()                                  # idle until preopen (9:15)
    assert eng.state == "idle" and ft.now == datetime(2026, 9, 22, 9, 15, tzinfo=ET)
    eng.tick()                                  # preopen work, wait until open
    assert eng.state == "preopen" and ft.now == OPEN and eng._preopen_done == D
    eng.tick()                                  # first cycle at 9:30:05
    assert eng.state == "running" and ft.now == OPEN + timedelta(seconds=5)
    assert len(st.cycles_for_day(D)) == 1
    eng.tick()
    assert ft.now == OPEN + timedelta(minutes=1, seconds=5) and len(st.cycles_for_day(D)) == 2
    ft.now = datetime(2026, 9, 22, 15, 54, 30, tzinfo=ET)
    eng.tick()                                  # next cycle would be at flatten_at → wait for it
    assert ft.now == datetime(2026, 9, 22, 15, 55, tzinfo=ET) and len(st.cycles_for_day(D)) == 2
    eng.tick()                                  # flatten
    assert eng.state == "postclose" and eng._flattened == D and broker.close_all_calls == 1
    eng.tick()                                  # wait until sweep_at
    assert ft.now == datetime(2026, 9, 22, 16, 30, tzinfo=ET)
    eng.tick()                                  # sweep
    assert sweeps == [D] and eng.state == "idle" and eng._swept == D
    eng.tick()                                  # idle, waits (capped at 10 min) for the next session
    assert eng.state == "idle" and ft.now == datetime(2026, 9, 22, 16, 40, tzinfo=ET)
    assert st.errors_count(where="unhandled") == 0


def test_run_forever_catches_everything(tmp_path, monkeypatch):
    eng, st, broker, ft = make_engine(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET))
    n = {"i": 0}

    def bad_tick():
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("loop bug")
        eng.stop()

    monkeypatch.setattr(eng, "tick", bad_tick)
    eng.run_forever()
    assert n["i"] == 2 and st.errors_count(where="unhandled") == 1


def test_next_cycle_time():
    eng = Engine.__new__(Engine)
    t = datetime(2026, 9, 22, 10, 0, 3, tzinfo=ET)
    assert Engine.next_cycle_time(eng, t) == datetime(2026, 9, 22, 10, 0, 5, tzinfo=ET)
    assert Engine.next_cycle_time(eng, t.replace(second=5)) == datetime(2026, 9, 22, 10, 1, 5, tzinfo=ET)
    assert Engine.next_cycle_time(eng, t.replace(second=40)) == datetime(2026, 9, 22, 10, 1, 5, tzinfo=ET)
