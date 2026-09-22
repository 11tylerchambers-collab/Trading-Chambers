"""Replay over synthetic days: dip-and-recover → one long vwap_touch; drift → stop_loss; flat → time_stop."""
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET, Session
from chambers.engine import Position, check_exit, trade_economics
from chambers.replay import ReplayBroker, build_day, format_replay, replay
from chambers.store import Bar
from chambers.strategy import Params

D = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
P = Params()


def bars_from(closes, volumes, start=OPEN):
    return [Bar(start + timedelta(minutes=i), c, c, c, c, v) for i, (c, v) in enumerate(zip(closes, volumes))]


def test_dip_and_recover_one_long_vwap_touch():
    # 30 flat bars at 100, then a dip to 99 on 5x volume (fires long), then recovery to 100.
    closes = [100.0] * 30 + [99.0] + [99.5, 100.2] + [100.0] * 10
    vols = [100.0] * 30 + [500.0] + [100.0] * 12
    day = build_day(D, {"AAPL": bars_from(closes, vols)})
    res = replay(day, P)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.side == "long" and t.exit_reason == "vwap_touch" and t.symbol == "AAPL"
    assert t.entry_price == 99.0 and t.qty == 20  # floor(2000/99)
    assert t.exit_price == 100.2 and t.bars_held == 2
    assert t.gross_pnl == pytest.approx((100.2 - 99.0) * 20)
    # cost: half spread 0.01% of price at each end × qty + 0.01% × notional × 2
    exp_cost = (99.0 * 0.0001 + 100.2 * 0.0001) * 20 + 0.0001 * 99.0 * 20 * 2
    assert t.est_cost == pytest.approx(exp_cost)
    assert t.net_pnl == pytest.approx(t.gross_pnl - exp_cost)
    assert t.mfe_pct == pytest.approx((100.2 - 99) / 99 * 100) and t.mae_pct == 0.0
    assert t.hypothesis["expect"] == "return to vwap" and t.hypothesis["dev_pct"] < 0
    assert t.entry_ts == OPEN + timedelta(minutes=31, seconds=5)  # cycle that saw bar 30
    assert "vwap_touch" in format_replay(res)


def test_drift_produces_stop_loss():
    # dip fires a long at 99, then price keeps falling: 98.4 is -0.606% → stop (0.50%)
    closes = [100.0] * 30 + [99.0, 98.8, 98.4, 98.0, 98.0]
    vols = [100.0] * 30 + [500.0, 100.0, 100.0, 100.0, 100.0]
    res = replay(build_day(D, {"X": bars_from(closes, vols)}), P)
    assert [t.exit_reason for t in res.trades][0] == "stop_loss"
    t = res.trades[0]
    assert t.exit_price == 98.4 and t.bars_held == 2 and t.gross_pnl < 0
    assert t.mae_pct == pytest.approx((98.4 - 99) / 99 * 100)


def test_flat_after_dip_produces_time_stop():
    # dip fires long at 99, then price sits at 99 (below vwap, above stop) for 15 bars → time_stop at 10
    closes = [100.0] * 30 + [99.0] + [99.0] * 15
    vols = [100.0] * 30 + [500.0] + [100.0] * 15
    res = replay(build_day(D, {"X": bars_from(closes, vols)}), P)
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t.exit_reason == "time_stop" and t.bars_held == 10 and t.exit_price == 99.0
    assert t.gross_pnl == 0.0 and t.net_pnl < 0  # only costs


def test_short_side_and_eod_flatten():
    # spike up fires a short; nothing else happens → eod flatten at C-5min
    session = Session(D, OPEN, OPEN + timedelta(minutes=60))  # early close at 10:30 so the day is short
    p = P.replace(max_hold_bars=100)
    closes = [100.0] * 30 + [101.0] + [100.9] * 29
    vols = [100.0] * 30 + [500.0] + [100.0] * 29
    res = replay(build_day(D, {"X": bars_from(closes, vols)}, session), p)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.side == "short" and t.exit_reason == "eod_flatten"
    assert t.exit_ts == session.flatten_at and t.exit_price == 100.9
    assert t.gross_pnl == pytest.approx((101.0 - 100.9) * t.qty)
    # allow_short=false → no trade at all
    res2 = replay(build_day(D, {"X": bars_from(closes, vols)}, session), p.replace(allow_short=False))
    assert res2.trades == [] and res2.signals_evaluated > 0 and res2.signals_fired == 0


def test_entries_window_respected():
    # a dip in the first 5 minutes can't fire (entries open at 9:35), but bars still accrue
    closes = [100.0] * 22 + [99.0] + [100.0] * 5
    vols = [100.0] * 22 + [500.0] + [100.0] * 5
    session = Session(D, OPEN, OPEN + timedelta(hours=6, minutes=30))
    # move the dip to minute 3 by starting bars 20 minutes before the open is not allowed; instead
    # build a session that opens at bar 20 so the dip (bar 22) is 2 minutes after open → entries closed
    late_session = Session(D, OPEN + timedelta(minutes=20), OPEN + timedelta(hours=6))
    bars = bars_from(closes, vols)
    day = build_day(D, {"X": bars}, late_session)
    # bars before the session open are dropped, so there are only 8 bars: insufficient anyway.
    assert day.bar_count == 8
    res = replay(day, P)
    assert res.trades == []
    # with the normal session the dip at minute 22 fires
    res2 = replay(build_day(D, {"X": bars}, session), P)
    assert len(res2.trades) == 1 and res2.trades[0].exit_reason == "vwap_touch"


def test_max_open_positions_limits_entries():
    closes = [100.0] * 30 + [99.0] + [99.0] * 3
    vols = [100.0] * 30 + [500.0] + [100.0] * 3
    bars = {s: bars_from(closes, vols) for s in ("A", "B", "C")}
    res = replay(build_day(D, bars), P.replace(max_open_positions=2, max_hold_bars=2))
    # only two of the three identical symbols can be entered on the dip bar
    entries = [t for t in res.trades if t.entry_ts == OPEN + timedelta(minutes=31, seconds=5)]
    assert len(entries) == 2


def test_check_exit_branches_directly():
    pos = Position(None, "X", "long", 10, 100.0, OPEN, OPEN)
    assert check_exit(pos, None, 100.0, P) is None
    assert check_exit(pos, 100.0, 99.5, P) == "vwap_touch"      # long, close >= vwap
    assert check_exit(pos, 99.6, 101.0, P) is None                # -0.4%: nothing yet
    assert check_exit(pos, 99.5, 101.0, P) == "stop_loss"         # -0.5% exactly hits stop_pct
    assert check_exit(pos, 99.5, 101.0, P.replace(stop_pct=0.6)) is None
    pos.bars_held = 10
    assert check_exit(pos, 99.9, 101.0, P) == "time_stop"
    assert check_exit(pos, 99.9, 101.0, P.replace(max_hold_bars=11)) is None
    s = Position(None, "X", "short", 10, 100.0, OPEN, OPEN)
    assert check_exit(s, 100.0, 100.5, P) == "vwap_touch"        # short, close <= vwap
    assert check_exit(s, 100.6, 100.0, P) == "stop_loss"
    assert check_exit(s, 100.4, 100.0, P) is None


def test_position_tracking_and_economics():
    pos = Position(None, "X", "long", 10, 100.0, OPEN, OPEN)
    assert pos.on_bar(OPEN, 99.0) is False          # entry bar does not count
    assert pos.on_bar(OPEN + timedelta(minutes=1), 99.0) is True and pos.bars_held == 1
    assert pos.on_bar(OPEN + timedelta(minutes=1), 98.0) is False  # same bar again is ignored
    pos.on_bar(OPEN + timedelta(minutes=2), 101.0)
    assert pos.mae_pct == pytest.approx(-1.0) and pos.mfe_pct == pytest.approx(1.0) and pos.bars_held == 2
    gross, cost, net = trade_economics("long", 10, 100.0, 101.0, 99.99, 100.01, 100.99, 101.01)
    assert gross == pytest.approx(10.0)
    assert cost == pytest.approx((0.01 + 0.01) * 10 + 0.0001 * 1000 * 2)
    assert net == pytest.approx(gross - cost)
    gross_s, cost_s, _ = trade_economics("short", 10, 100.0, 101.0, None, None, None, None)
    assert gross_s == pytest.approx(-10.0) and cost_s == pytest.approx(0.2)  # no quotes → slippage only


def test_replay_broker_spread():
    b = ReplayBroker()
    bid, ask = b.quote(100.0)
    assert ask - bid == pytest.approx(0.02) and b.fill(100.0) == 100.0


def test_reentry_cooldown_after_stop_loss():
    # long fires at 99 (bar 30); bar 31 at 98.4 stops it out while still deviated on heavy volume.
    closes = [100.0] * 30 + [99.0] + [98.4] * 10
    vols = [100.0] * 30 + [500.0] * 11
    day = build_day(D, {"X": bars_from(closes, vols)})
    # cooldown off: re-entered on the very bar that stopped it out
    r0 = replay(day, P.replace(reentry_cooldown_bars=0))
    assert r0.trades[0].exit_reason == "stop_loss"
    assert r0.trades[1].entry_ts == OPEN + timedelta(minutes=32, seconds=5)
    # default cooldown of 5 bars: exit at bar count 32, next entry no earlier than bar count 37 (bar 36)
    r5 = replay(day, P)
    assert P.reentry_cooldown_bars == 5
    assert r5.trades[0].exit_reason == "stop_loss" and r5.trades[0].exit_ts == OPEN + timedelta(minutes=32, seconds=5)
    assert r5.trades[1].entry_ts == OPEN + timedelta(minutes=37, seconds=5)
    assert r5.signals_fired == len(r5.trades) == 2
