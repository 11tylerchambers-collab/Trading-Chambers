"""S1, S2, S3: strategy branches, live cycles with the mock broker, schedules, restart, replay and sweep."""
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from chambers.bars import TF_1H, TF_S4H, build_15m
from chambers.clock import ET, Session
from chambers.engine import Position
from chambers.scheduler import Scheduler
from chambers.sleeve import BarSleeve
from chambers.sleeve_replay import (load_window, one_step, replay_bars, run_bar_sweep, s2_events)
from chambers.store import Bar, Store
from chambers.strategies import S1, S2, S3, evaluate_entry, s1_entry, s1_exit, s2_entry, s2_exit, s3_entry, s3_exit

from .mocks import FakeTime, MockBroker, make_clock

UTC = timezone.utc
D0, D1, D2 = date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)


def sess(d, close_h=16):
    return (d, datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET), datetime(d.year, d.month, d.day, close_h, 0, tzinfo=ET))


SESSIONS = [sess(D0), sess(D1), sess(D2)]


def bar(ts, c, v=100.0, rng=0.05):
    return Bar(ts, c, c + rng, c - rng, c, v)


def wiggle(i):
    return 0.03 * math.sin(i * 1.3)


# ======================================================================== pure strategy branches

def series(closes, start=datetime(2026, 9, 1, 10, 0, tzinfo=ET), step=timedelta(minutes=15), vols=None, rng=0.05):
    return [bar(start + i * step, c, (vols[i] if vols else 100.0), rng) for i, c in enumerate(closes)]


def test_s1_entry_branches():
    p = S1.params()
    assert s1_entry(series([100.0] * 10), p).reason == "insufficient_bars"
    flat = [100 + wiggle(i) for i in range(30)]
    assert s1_entry(series(flat), p).reason == "z_too_small"
    ev = s1_entry(series(flat + [99.0]), p)
    assert ev.fired and ev.side == "long" and ev.detail["z"] <= -2 and ev.stop_distance == pytest.approx(2 * ev.detail["atr14"], rel=1e-3)
    ev = s1_entry(series(flat + [101.0]), p)
    assert ev.fired and ev.side == "short"
    # gates in order
    h = series(flat + [99.0])
    assert evaluate_entry(S1, h, p, False, None, False).reason == "entries_closed"
    assert evaluate_entry(S1, h, p, True, None, True).reason == "already_in_position"
    assert evaluate_entry(S1, h, p, False, 1, True).reason == "cooldown"
    assert evaluate_entry(S1, h, p, False, 2, True).reason == "fired"
    assert evaluate_entry(S1, series([100.0] * 5), p, True, None, False).reason == "entries_closed"


def test_s1_exit_branches():
    p = S1.params()
    flat = [100 + wiggle(i) for i in range(30)]
    long = Position(1, "SPY", "long", 10, 99.0, None, None, stop_price=98.0)
    assert s1_exit(long, series(flat + [99.2]), p) is None
    assert s1_exit(long, series(flat + [100.2]), p) == "z_revert"
    assert s1_exit(long, series(flat + [97.9]), p) == "stop_loss"
    long.bars_held = 8
    assert s1_exit(long, series(flat + [99.2]), p) == "time_stop"
    short = Position(1, "SPY", "short", 10, 101.0, None, None, stop_price=102.0)
    assert s1_exit(short, series(flat + [99.9]), p) == "z_revert"
    assert s1_exit(short, series(flat + [102.1]), p) == "stop_loss"


def test_s2_entry_exit_branches():
    p = S2.params()
    base = [100000 + 50 * math.sin(i) for i in range(40)]
    step = timedelta(hours=1)
    assert s2_entry(series(base[:20], step=step), p).reason == "insufficient_bars"
    assert s2_entry(series(base, step=step), p).reason == "no_breakout"
    assert s2_entry(series(base + [100500], step=step), p).reason == "vol_too_low"
    ev = s2_entry(series(base + [100500], step=step, vols=[100.0] * 40 + [200.0]), p)
    assert ev.fired and ev.side == "long" and ev.detail["vol_ratio"] == pytest.approx(2.0)
    pos = Position(1, "BTC/USD", "long", 0.1, 100500, None, None, stop_price=100000)
    assert s2_exit(pos, series(base + [100100], step=step), p) is None
    assert s2_exit(pos, series(base + [99900], step=step), p) == "channel_exit"
    pos.stop_price = 100200
    assert s2_exit(pos, series(base + [100100], step=step), p) == "stop_loss"
    # long only: a short never comes out of S2
    assert S2.allow_short is False


def test_s3_entry_exit_branches():
    p = S3.params()
    up = [100 + 0.5 * i for i in range(60)]
    step = timedelta(hours=4)
    assert s3_entry(series(up[:20], step=step), p).reason == "insufficient_bars"
    ev = s3_entry(series(up, step=step), p)
    assert ev.fired and ev.side == "long" and ev.detail["ema_fast"] > ev.detail["ema_slow"]
    down = [130 - 0.5 * i for i in range(60)]
    assert s3_entry(series(down, step=step), p).side == "short"
    assert s3_entry(series(up + [up[-1] - 6], step=step), p).reason == "no_trend"   # dipped below the fast EMA
    pos = Position(1, "GLD", "long", 10, 120.0, None, None, stop_price=110.0)
    assert s3_exit(pos, series(up, step=step), p) is None
    crash = up + [up[-1] - 3 * k for k in range(1, 15)]
    assert s3_exit(Position(1, "GLD", "long", 10, 120.0, None, None, stop_price=50.0), series(crash, step=step), p) \
        == "trend_reversal"
    assert s3_exit(Position(1, "GLD", "long", 10, 130.0, None, None, stop_price=129.6), series(up, step=step), p) \
        == "stop_loss"
    with pytest.raises(ValueError):
        S3.validate(S3.params({"fast": 30, "slow": 20}))


def test_one_step_rule_and_s3_constraint():
    cur = S1.params()
    assert one_step(S1, cur, {**cur, "entry_z": 3.0, "stop_atr": 1.5, "max_hold_bars": 12}) == \
        {**cur, "entry_z": 2.5, "stop_atr": 1.5, "max_hold_bars": 12}
    # every one-step move between valid S3 combinations stays valid (fast < slow)
    for cur in S3.grid_combos(S3.params()):
        for best in S3.grid_combos(S3.params()):
            out = one_step(S3, cur, best)
            assert out["fast"] < out["slow"]
            assert all(abs(S3.grid[k].index(out[k]) - S3.grid[k].index(cur[k])) <= 1 for k in S3.grid)


# ======================================================================== S1 live

def s1_minutes(d, drop_at=None, drop_to=98.0, recover_at=None, base=100.0, extra_drop=None):
    o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    out = []
    for i in range(390):
        c = base + wiggle(i)
        if drop_at is not None and drop_at <= i < (recover_at or 999):
            c = drop_to
        out.append(bar(o + timedelta(minutes=i), c))
    return out


def s1_setup(tmp_path, now, today_spy, today_qqq=None, capital=20000.0):
    ft = FakeTime(now)
    broker = MockBroker(sessions=SESSIONS, bars={"SPY": today_spy, "QQQ": today_qqq or s1_minutes(D1, base=400.0)})
    st = Store(tmp_path / "t.db")
    st.write_bars("SPY", s1_minutes(D0))
    st.write_bars("QQQ", s1_minutes(D0, base=400.0))
    clk = make_clock(ft, broker)
    sl = BarSleeve(S1, st, broker, clk, {}, sleep_fn=ft.sleep, capital=capital, fetch_history=False)
    return sl, st, broker, ft


def test_s1_live_entry_sizing_then_z_revert(tmp_path):
    spy = s1_minutes(D1, drop_at=30, recover_at=45)             # 10:00-10:14 at 98, back to 100 at 10:15
    sl, st, broker, ft = s1_setup(tmp_path, datetime(2026, 9, 22, 10, 15, 5, tzinfo=ET), spy)
    broker.prices["SPY"] = 98.0
    sl.startup()
    res = sl.run_cycle(datetime(2026, 9, 22, 10, 15, 5, tzinfo=ET))
    assert res.reasons == {"fired": 1, "z_too_small": 1} and res.errors == 0
    t = st.open_trades("S1")[0]
    hyp = t.hypothesis
    assert t.sleeve_id == "S1" and t.side == "long" and t.symbol == "SPY" and t.news_day == 0
    # 1% of $20k = $200 / (2 × ATR) floored, capped at 50% of $20k / $98 = 102 shares
    atr = hyp["atr14"]
    assert t.qty == min(math.floor(200 / (2 * atr)), math.floor(10000 / 98.0))
    assert hyp["stop_price"] == pytest.approx(98.0 - 2 * atr) and hyp["entry_bar_ts"].startswith("2026-09-22T10:00")
    sig = [s for s in st.signals_for_day(D1, "S1") if s["symbol"] == "SPY"][0]
    assert sig["fired"] == 1 and '"z"' in sig["detail_json"]
    # next bar recovers to 100 → z crosses 0 → exit
    broker.prices["SPY"] = 100.0
    ft.now = datetime(2026, 9, 22, 10, 30, 5, tzinfo=ET)
    res = sl.run_cycle(ft.now)
    c = st.closed_trades_for_day(D1, "S1")
    assert len(c) == 1 and c[0].exit_reason == "z_revert" and c[0].bars_held == 1 and c[0].exit_bar_ts is not None
    assert res.reasons.get("cooldown") == 1                      # SPY cannot re-enter on the exit bar
    hb = st.read_heartbeat("S1")
    assert hb["cycles_today"] == 2 and hb["closed_today"] == 1


def test_s1_pair_filter_blocks_same_direction(tmp_path):
    spy = s1_minutes(D1, drop_at=30, recover_at=45)
    qqq = s1_minutes(D1, drop_at=30, recover_at=45, base=400.0, drop_to=392.0)
    sl, st, broker, ft = s1_setup(tmp_path, datetime(2026, 9, 22, 10, 15, 5, tzinfo=ET), spy, qqq)
    sl.startup()
    res = sl.run_cycle(datetime(2026, 9, 22, 10, 15, 5, tzinfo=ET))
    assert res.reasons == {"fired": 1, "pair_filter": 1}
    assert [t.symbol for t in st.open_trades("S1")] == ["SPY"]
    assert [s["reason"] for s in st.signals_for_day(D1, "S1")] == ["fired", "pair_filter"]


def test_s1_schedule_and_eod_flatten(tmp_path):
    spy = s1_minutes(D1, drop_at=360, drop_to=98.0)             # 15:30 drop: entry at 15:45:05? entries closed
    sl, st, broker, ft = s1_setup(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET), spy)
    sch = Scheduler(st, broker, sl.clock, [sl], sleep_fn=ft.sleep)
    sch.startup()
    while ft.now < datetime(2026, 9, 22, 16, 40, tzinfo=ET):
        sch.tick()
    cyc = st.cycles_for_day(D1, "S1")
    assert [c["ts"][11:19] for c in cyc][:2] == ["09:45:05", "10:00:05"] and len(cyc) == 25
    assert cyc[-1]["ts"][11:19] == "15:45:05"
    # 15:45:05 is outside the entry window (close − 15 min): the 15:30 drop is logged, not traded
    last = [s for s in st.signals_for_day(D1, "S1") if s["ts"][11:19] == "15:45:05"]
    assert {s["reason"] for s in last} == {"entries_closed"}
    assert sl._flattened == D1 and st.has_sweep_for(D1, "S1")


def test_s1_eod_flatten_closes_open_position(tmp_path):
    spy = s1_minutes(D1, drop_at=300, drop_to=98.0)              # 14:30 drop, entry at 14:45:05, stays down
    sl, st, broker, ft = s1_setup(tmp_path, datetime(2026, 9, 22, 14, 44, tzinfo=ET), spy)
    broker.prices["SPY"] = 98.0
    sch = Scheduler(st, broker, sl.clock, [sl], sleep_fn=ft.sleep)
    sch.startup()
    while ft.now < datetime(2026, 9, 22, 15, 56, tzinfo=ET):
        sch.tick()
    trades = st.closed_trades_for_day(D1, "S1")
    assert trades and trades[-1].exit_reason in ("eod_flatten", "time_stop", "stop_loss")
    assert st.open_trades("S1") == [] and broker.positions_ == {}


# ======================================================================== S2 live, 24/7

def btc_hours(start, n, breakout_at=None, level=100000.0):
    out = []
    for i in range(n):
        c = level + 60 * math.sin(i * 0.9)
        v = 10.0
        if breakout_at is not None and i >= breakout_at:
            c = level + 800 if i < breakout_at + 3 else level - 400
            v = 30.0 if i == breakout_at else 10.0
        out.append(Bar(start + timedelta(hours=i), c, c + 40, c - 40, c, v))
    return out


def s2_setup(tmp_path, now, bars):
    ft = FakeTime(now)
    broker = MockBroker(sessions=SESSIONS)
    broker.tf_bars[("BTC/USD", 60)] = bars
    st = Store(tmp_path / "t.db")
    clk = make_clock(ft, broker)
    sl = BarSleeve(S2, st, broker, clk, {}, sleep_fn=ft.sleep, capital=20000.0, fee_rate=0.0025)
    return sl, st, broker, ft


def test_s2_breakout_fractional_long_then_channel_exit(tmp_path):
    start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    bars = btc_hours(start, 72, breakout_at=60)
    brk_end = start + timedelta(hours=61)
    sl, st, broker, ft = s2_setup(tmp_path, (brk_end + timedelta(seconds=10)).astimezone(ET), bars)
    broker.prices["BTC/USD"] = 100800.0
    sl.startup()                                                   # fetches 35 days of history (what exists)
    res = sl.run_cycle(brk_end + timedelta(seconds=10))
    assert res.reasons == {"fired": 1}
    t = st.open_trades("S2")[0]
    assert t.symbol == "BTC/USD" and t.side == "long" and 0 < t.qty < 1 and t.qty != int(t.qty)
    assert t.qty * 100800.0 <= 10000.0 + 1e-6                        # 50% notional cap
    assert broker.submitted[-1]["qty"] == t.qty
    # three hours later price falls under the 12-bar channel low → exit, crypto fee in the costs
    broker.prices["BTC/USD"] = 99600.0
    ft.now = (start + timedelta(hours=64, seconds=10)).astimezone(ET)
    sl.run_cycle(start + timedelta(hours=64, seconds=10))
    c = st.all_closed_trades("S2")[0]
    assert c.exit_reason == "channel_exit"
    fees = 0.0025 * (c.entry_price + c.exit_price) * c.qty
    assert c.net_pnl == pytest.approx(c.gross_pnl - fees - 0.0001 * c.entry_price * c.qty * 2)


def test_s2_runs_every_hour_across_midnight_utc_and_sweeps_at_0030(tmp_path):
    start = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
    bars = btc_hours(start, 80)
    now = datetime(2026, 9, 22, 22, 0, 0, tzinfo=UTC)
    sl, st, broker, ft = s2_setup(tmp_path, now.astimezone(ET), bars)
    sch = Scheduler(st, broker, sl.clock, [sl], sleep_fn=ft.sleep)
    sch.startup()
    while ft.now < datetime(2026, 9, 23, 3, 30, tzinfo=UTC).astimezone(ET):
        sch.tick()
    ts = [c["ts"] for c in st._query("SELECT ts FROM cycles WHERE sleeve_id='S2' ORDER BY id")]
    hours = [datetime.fromisoformat(t).astimezone(UTC).strftime("%d %H:%M:%S") for t in ts]
    assert hours == ["22 22:00:10", "22 23:00:10", "23 00:00:10", "23 01:00:10", "23 02:00:10", "23 03:00:10",
                     "23 04:00:10"]
    assert st.has_sweep_for("2026-09-22", "S2")                     # the UTC day that ended, swept at 00:30
    # startup caught up the missed sweep for 09-21 once; each UTC day has exactly one row
    assert sorted(r["date"] for r in st.params_history(10, "sweep", "S2")) == ["2026-09-21", "2026-09-22"]


# ======================================================================== S3 live, overnight

def s3_setup(tmp_path, now, capital=20000.0):
    ft = FakeTime(now)
    broker = MockBroker(sessions=SESSIONS)
    st = Store(tmp_path / "t.db")
    # 60 stored session-4h bars in a steady uptrend for GLD, flat USO
    for sym, f in (("GLD", lambda i: 180 + 0.4 * i), ("USO", lambda i: 70 + 0.2 * math.sin(i))):
        bs = []
        d = date(2026, 6, 1)
        i = 0
        while i < 60:
            if d.weekday() < 5:
                o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
                bs.append(Bar(o, f(i), f(i) + 0.3, f(i) - 0.3, f(i), 1000))
                bs.append(Bar(o + timedelta(hours=4), f(i + 1), f(i + 1) + 0.3, f(i + 1) - 0.3, f(i + 1), 800))
                i += 2
            d += timedelta(days=1)
        st.write_bars_tf(sym, TF_S4H, bs)
    o = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    broker.bars["GLD"] = [bar(o + timedelta(minutes=i), 204.5 + 0.001 * i) for i in range(390)]
    broker.bars["USO"] = [bar(o + timedelta(minutes=i), 70.0) for i in range(390)]
    broker.prices.update({"GLD": 204.7, "USO": 70.0})
    clk = make_clock(ft, broker)
    sl = BarSleeve(S3, st, broker, clk, {}, sleep_fn=ft.sleep, capital=capital, fetch_history=False)
    return sl, st, broker, ft


def test_s3_decides_at_1330_and_flatten_at_and_holds_overnight(tmp_path):
    sl, st, broker, ft = s3_setup(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET))
    sch = Scheduler(st, broker, sl.clock, [sl], sleep_fn=ft.sleep)
    sch.startup()
    while ft.now < datetime(2026, 9, 22, 16, 40, tzinfo=ET):
        sch.tick()
    cyc = [c["ts"][11:19] for c in st.cycles_for_day(D1, "S3")]
    assert cyc == ["13:30:05", "15:55:00"]
    t = st.open_trades("S3")
    assert [x.symbol for x in t] == ["GLD"] and t[0].side == "long"
    assert broker.positions_.get("GLD") == t[0].qty                  # not flattened at the close
    assert sl._flattened is None and st.has_sweep_for(D1, "S3")
    stored = st.bars_tf("GLD", TF_S4H, datetime(2026, 9, 22, tzinfo=ET))
    assert [b.ts.strftime("%H:%M") for b in stored] == ["09:30", "13:30"]   # finalized after the close
    # next morning: a fresh process adopts the overnight position through reconcile
    ft.now = datetime(2026, 9, 23, 9, 20, tzinfo=ET)
    sl2 = BarSleeve(S3, st, broker, sl.clock, {}, sleep_fn=ft.sleep, fetch_history=False)
    sch2 = Scheduler(st, broker, sl.clock, [sl2], sleep_fn=ft.sleep)
    sch2.startup()
    assert "GLD" in sl2.positions and sl2.positions["GLD"].stop_price == t[0].hypothesis["stop_price"]
    assert sl2.positions["GLD"].bars_held == 1                       # the 13:30 bar came after the entry bar (09:30)
    assert broker.submitted[-1]["symbol"] == "GLD" and len([o for o in broker.submitted if o["side"] == "sell"]) == 0
    assert st.errors_count(where="reconcile") == 0


# ======================================================================== replay == live, sweep

def test_s1_replay_matches_live_entries(tmp_path):
    spy = s1_minutes(D1, drop_at=30, recover_at=45)
    sl, st, broker, ft = s1_setup(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET), spy)
    broker.prices["SPY"] = 98.0
    sch = Scheduler(st, broker, sl.clock, [sl], sleep_fn=ft.sleep)
    sch.startup()
    while ft.now < datetime(2026, 9, 22, 11, 0, tzinfo=ET):
        if ft.now >= datetime(2026, 9, 22, 10, 16, tzinfo=ET):
            broker.prices["SPY"] = 100.0
        sch.tick()
    live = st.all_closed_trades("S1")
    sessions = {d: Session(d, o, c) for d, o, c in SESSIONS}
    series, events, dates = load_window(S1, st, D1, sessions)
    events = [e for e in events if e.t < datetime(2026, 9, 22, 11, 0, tzinfo=ET)]
    rep = replay_bars(S1, series, events, S1.params(), 20000.0, close_at_end=False)
    assert [(t.symbol, t.side, t.qty, t.entry_ts, t.exit_ts, t.exit_reason) for t in rep.trades] == \
           [(t.symbol, t.side, t.qty, datetime.fromisoformat(t.entry_ts), datetime.fromisoformat(t.exit_ts),
             t.exit_reason) for t in live]


def test_s2_sweep_evidence_eligibility_and_idempotency(tmp_path):
    st = Store(tmp_path / "t.db")
    start = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(24 * 34):
        cyc = i % 30
        c = 100000 + (900 if 24 <= cyc < 27 else -300 if cyc >= 27 else 40 * math.sin(i))
        bars.append(Bar(start + timedelta(hours=i), c, c + 30, c - 30, c, 30.0 if cyc == 24 else 10.0))
    st.write_bars_tf("BTC/USD", TF_1H, bars)
    now = datetime(2026, 9, 23, 0, 30, tzinfo=UTC).astimezone(ET)
    thin = run_bar_sweep(st, S2, S2.params({"vol_mult": 2.0, "lookback": 48}), date(2026, 9, 2), now, 20000.0, 0.0025)
    assert thin["reason"].startswith(("insufficient_evidence", "no_data"))
    s = run_bar_sweep(st, S2, S2.params(), date(2026, 9, 22), now, 20000.0, 0.0025)
    assert s["evidence_trades"] >= 20 and s["combos_evaluated"] == 81
    assert s["reason"].split(":")[0] in ("moved_one_step_toward_best", "moved_to_best", "best_is_current")
    for k in S2.grid:
        vals = S2.grid[k]
        assert abs(vals.index(s["chosen"][k]) - vals.index(S2.params()[k])) <= 1
    assert st.params_history(5, "sweep", "S2")[0]["sweep_summary"]["reason"] == s["reason"]
    again = run_bar_sweep(st, S2, S2.params(), date(2026, 9, 22), now, 20000.0, 0.0025)
    assert again["reason"].startswith("already_swept") and len(st.params_history(10, "sweep", "S2")) == 2
