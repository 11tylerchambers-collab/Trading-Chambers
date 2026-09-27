"""Random twins: reproducible from the logged seed, live == replay, restart-safe, p from history (§4)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from chambers.clock import ET
from chambers.engine import Engine
from chambers.replay import build_day
from chambers.runtime import s0_fallback_rate
from chambers.sleeve_replay import replay_bars, s2_events
from chambers.store import Bar, Store
from chambers.strategies import S2
from chambers.strategy import Params
from chambers.twin import LiveTwin, TwinBook, day_seed, draw, edge, replay_twin_s0, trailing_rate

from .mocks import UNIVERSE, FakeTime, MockBroker, make_clock
from .test_migration import synthetic_day

D = date(2026, 9, 24)
OPEN = datetime(2026, 9, 24, 9, 30, tzinfo=ET)
P = Params()


def key(trades):
    return [(t.symbol, t.side, t.qty, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.exit_reason,
             round(t.net_pnl, 9)) for t in trades]


def test_draw_is_a_pure_function_of_seed_symbol_bar():
    ts = OPEN + timedelta(minutes=40)
    assert draw(123, "AAPL", ts) == draw(123, "AAPL", ts)
    assert draw(123, "AAPL", ts) != draw(124, "AAPL", ts) != draw(123, "MSFT", ts)
    assert day_seed("S1", "2026-09-24") == day_seed("S1", "2026-09-24") != day_seed("S2", "2026-09-24")


def test_s0_twin_day_reproducible_from_seed():
    day = build_day(D, dict(sorted(synthetic_day().items())))
    a = replay_twin_s0(day, P, 42, 0.02)
    b = replay_twin_s0(day, P, 42, 0.02)
    c = replay_twin_s0(day, P, 43, 0.02)
    assert len(a.trades) > 10 and key(a.trades) == key(b.trades) and key(a.trades) != key(c.trades)
    assert {t.side for t in a.trades} == {"long", "short"}
    assert all(t.est_cost > 0 for t in a.trades)                     # costs included
    assert replay_twin_s0(day, P, 42, 0.0).trades == []


def run_live_s0(tmp_path, stop_at=None, db="t.db", restart_at=None):
    bars = synthetic_day()
    broker = MockBroker(sessions=[(D, OPEN, OPEN.replace(hour=16, minute=0))], bars=bars)
    st = Store(tmp_path / db)
    st.set_twin_seed("S0", D.isoformat(), 42, 0.02, "test", OPEN)
    ft = FakeTime(OPEN + timedelta(seconds=5))

    def make():
        clk = make_clock(ft, broker)
        eng = Engine(st, broker, clk, UNIVERSE, P.to_dict(), sleep_fn=ft.sleep)
        eng.twin = LiveTwin(eng, allow_short=True)
        eng.startup()
        return eng

    eng = make()
    while ft.now < OPEN.replace(hour=15, minute=55):
        if restart_at is not None and ft.now >= restart_at:
            eng = make()                                             # a new process: twin restored from the store
            restart_at = None
        broker.prices = {s: next((b.c for b in reversed(bars[s]) if b.ts < ft.now.replace(second=0)), 100.0)
                         for s in UNIVERSE}
        eng.run_cycle()
        ft.advance(minutes=1)
    eng.twin.eod(eng, ft.now)
    return st


def test_live_twin_equals_replay_twin_and_survives_restart(tmp_path):
    st = run_live_s0(tmp_path)
    live = st.all_closed_twin_trades()
    assert len(live) > 10 and all(t["seed"] == 42 for t in live)
    assert all(t["news_day"] == 0 for t in live) and all(t["net_pnl"] is not None for t in live)
    day = build_day(D, dict(sorted(synthetic_day().items())))
    rep = replay_twin_s0(day, P, 42, 0.02)
    live_key = sorted((t["symbol"], t["side"], t["entry_ts"], t["exit_reason"], round(t["net_pnl"], 6)) for t in live)
    rep_key = sorted((t.symbol, t.side, t.entry_ts.isoformat(), t.exit_reason, round(t.net_pnl, 6))
                     for t in rep.trades)
    assert live_key == rep_key
    # the same day with a restart in the middle gives the same twin trades
    st2 = run_live_s0(tmp_path, db="t2.db", restart_at=OPEN.replace(hour=12, minute=0, second=5))
    k2 = sorted((t["symbol"], t["side"], t["entry_ts"], t["exit_reason"], round(t["net_pnl"], 6))
                for t in st2.all_closed_twin_trades())
    assert k2 == live_key


def test_bar_sleeve_twin_reproducible():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bars = [Bar(start + timedelta(hours=i), 100000 + 300 * ((i // 7) % 3), 100000 + 300 * ((i // 7) % 3) + 80,
                100000 + 300 * ((i // 7) % 3) - 80, 100000 + 300 * ((i // 7) % 3), 10) for i in range(24 * 10)]
    series = {"BTC/USD": bars}
    events = s2_events(series)
    run = lambda: replay_bars(S2, series, events, S2.params(), 20000.0, 0.0025,
                              twin_seed_for=lambda d: day_seed("S2", d), twin_p=0.05)
    a, b = run(), run()
    assert len(a.twin.trades) > 3
    assert [(t.entry_ts, t.exit_ts, t.net_pnl) for t in a.twin.trades] == \
           [(t.entry_ts, t.exit_ts, t.net_pnl) for t in b.twin.trades]
    assert all(t.side == "long" for t in a.twin.trades)             # S2 is long only, so is its twin
    assert {t.seed for t in a.twin.trades} == {day_seed("S2", d) for d in {t.day for t in a.twin.trades}}


def test_p_from_trailing_signals_then_fallback(tmp_path):
    st = Store(tmp_path / "t.db")
    rows = []
    for d in range(1, 4):
        ts = datetime(2026, 9, 20 + d, 10, 0, tzinfo=ET)
        rows += [{"ts": ts, "symbol": "SPY", "reason": "fired", "fired": True}]
        rows += [{"ts": ts, "symbol": "SPY", "reason": "z_too_small"}] * 9
        rows += [{"ts": ts, "symbol": "SPY", "reason": "entries_closed"}] * 50       # not eligible
        rows += [{"ts": ts, "symbol": "SPY", "reason": "already_in_position"}] * 5   # not eligible
    st.write_signals(1, rows, "S1")
    p, fired, elig = trailing_rate(st, "S1", "2026-09-24")
    assert (fired, elig) == (3, 30) and p == pytest.approx(0.1)
    assert trailing_rate(st, "S1", "2026-09-22")[1:] == (1, 10)     # only days before
    # no history → fallback, and the source is logged with the seed
    eng = Engine(st, MockBroker(), make_clock(FakeTime(OPEN), MockBroker()), ["AAPL"], P.to_dict())
    tw = LiveTwin(eng, True, fallback_rate=lambda: 0.033)
    tw.ensure_day(OPEN)
    row = st.get_twin_seed("S0", D.isoformat())
    assert row["p"] == 0.033 and row["seed"] == day_seed("S0", D.isoformat()) and "replay" in row["p_source"]
    assert tw.book.seed == row["seed"]


def test_s0_fallback_rate_uses_latest_stored_day(tmp_path):
    st = Store(tmp_path / "t.db")
    for s, bs in synthetic_day().items():
        st.write_bars(s, bs)
    eng = Engine(st, MockBroker(), make_clock(FakeTime(OPEN + timedelta(days=1)), MockBroker()), UNIVERSE, P.to_dict())
    p = s0_fallback_rate(eng)
    assert 0 < p < 1


def test_edge_is_sleeve_minus_twin(tmp_path):
    st = Store(tmp_path / "t.db")
    for i, d in enumerate(["2026-09-22", "2026-09-23", "2026-09-24"]):
        ts = datetime.fromisoformat(d + "T10:00:00-04:00")
        st.write_cycle(ts, "running", 2, 0, 0, 0, 5, "S1")
        tid = st.open_trade("SPY", "long", 10, ts, 100.0, "o", None, None, {}, {}, sleeve_id="S1")
        st.close_trade(tid, ts + timedelta(minutes=30), 101.0, "x", None, None, "z_revert", 2, 0, 1, 10.0, 0.2, 9.8 + i)
        twid = st.open_twin_trade("S1", d, 1, "SPY", "short", 10, ts, 100.0, {}, {}, False, None)
        st.close_twin_trade(twid, ts + timedelta(minutes=30), 101.0, "z_revert", 2, -1, 0, -10.0, 0.2, -10.2)
    e = edge(st, "S1", "2026-09-24")
    assert e["sleeve_net"] == 11.8 and e["twin_net"] == -10.2 and e["edge"] == 22.0
    assert e["sessions"] == 3 and e["edge_trailing"] == pytest.approx((9.8 + 10.8 + 11.8) + 3 * 10.2)
