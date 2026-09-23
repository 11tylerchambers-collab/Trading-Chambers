"""Sweep: one-step rule, eligibility, evidence rule, params_history written."""
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.store import Bar, Store
from chambers.strategy import Params
from chambers.sweep import GRID, evaluate_grid, grid_combos, load_days, nearest_index, one_step, run_sweep

NOW = datetime(2026, 9, 23, 16, 30, tzinfo=ET)


def test_one_step_rule():
    cur = Params(entry_dev_pct=0.30, vol_mult=1.50, max_hold_bars=10, stop_pct=0.50)
    best = Params(entry_dev_pct=0.80, vol_mult=1.00, max_hold_bars=15, stop_pct=0.50)
    nxt = one_step(cur, best)
    assert nxt.entry_dev_pct == 0.40      # +1 step of 0.30 → 0.40, not 0.80
    assert nxt.vol_mult == 1.25           # -1 step
    assert nxt.max_hold_bars == 15        # exactly one step away → reaches it
    assert nxt.stop_pct == 0.50           # unchanged
    assert one_step(cur, cur) == cur
    # non-grid current value snaps to nearest grid point first
    off = cur.replace(entry_dev_pct=0.33)
    assert one_step(off, best).entry_dev_pct == 0.40
    # other params (allow_short etc.) are carried through untouched
    assert one_step(cur.replace(allow_short=False), best).allow_short is False


def test_nearest_index_and_grid_size():
    assert nearest_index(GRID["entry_dev_pct"], 0.30) == 1
    assert nearest_index(GRID["entry_dev_pct"], 0.34) == 1
    assert nearest_index(GRID["entry_dev_pct"], 0.36) == 2
    assert nearest_index(GRID["entry_dev_pct"], 5.0) == 5
    combos = grid_combos(Params())
    assert len(combos) == 6 * 4 * 4 * 3 == 288 and len(set(combos)) == 288


def synthetic_day(store: Store, d: date, symbols, n_bars=390, dip_every=6, dip_pct=0.6):
    """Every `dip_every` bars a symbol dips by dip_pct on 5x volume, then recovers → many vwap_touch trades.
    A -0.6% dip fires at entry_dev_pct 0.20..0.50 but not at 0.60/0.80."""
    open_ = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    for k, s in enumerate(symbols):
        bars = []
        px = 100.0 + k
        for i in range(n_bars):
            c, v = px, 100.0
            if i >= 25 and i % dip_every == 0:
                c, v = px * (1 - dip_pct / 100), 500.0
            bars.append(Bar(open_ + timedelta(minutes=i), c, c, c, c, v))
        store.write_bars(s, bars)


def seed_closed_trades(store: Store, d: date, n: int):
    ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=ET)
    for i in range(n):
        tid = store.open_trade("AAPL", "long", 1, ts, 100.0, None, None, None, {}, {})
        store.close_trade(tid, ts + timedelta(minutes=i + 1), 100.1, None, None, None, "vwap_touch", 1, 0, 0.1, 0.1, 0.02, 0.08)


def test_sweep_on_two_synthetic_days_enforces_one_step_and_writes_history(tmp_path):
    st = Store(tmp_path / "t.db")
    d1, d2 = date(2026, 9, 22), date(2026, 9, 23)
    syms = [f"S{i}" for i in range(4)]
    synthetic_day(st, d1, syms)
    synthetic_day(st, d2, syms)
    seed_closed_trades(st, d2, 25)
    # current params sit at the far end of the grid so the best (small dev threshold) is >1 step away
    cur = Params(entry_dev_pct=0.80, vol_mult=2.00, max_hold_bars=20, stop_pct=0.75)
    summary = run_sweep(st, cur, d2, NOW)
    assert summary["days"] == [d1.isoformat(), d2.isoformat()]
    assert summary["combos_evaluated"] == 288 and summary["eligible"] > 0
    assert summary["best"]["entry_dev_pct"] <= 0.50            # the dips are 0.6%
    assert summary["changed"] is True
    chosen = summary["chosen"]
    assert chosen["entry_dev_pct"] == 0.60                      # one step from 0.80, not all the way
    for k in GRID:
        ci, bi, ni = (nearest_index(GRID[k], x) for x in (getattr(cur, k), summary["best"][k], chosen[k]))
        assert abs(ni - ci) <= 1 and (ni == ci or (ni - ci) * (bi - ci) > 0)
    assert summary["reason"].startswith("moved_one_step_toward_best")
    # written to params (source=sweep) and params_history
    live = st.read_params()
    assert live["source"] == "sweep" and live["params"]["entry_dev_pct"] == 0.60
    hist = st.params_history(7, source="sweep")
    assert len(hist) == 1 and hist[0]["date"] == d2.isoformat() and hist[0]["params"] == chosen
    ss = hist[0]["sweep_summary"]
    assert len(ss["results"]) == 288 and ss["best"] == summary["best"] and ss["today_closed_trades"] == 25
    assert all({"params", "net_pnl", "trades", "trades_per_day", "eligible"} <= set(r) for r in ss["results"])


def test_sweep_does_not_change_params_without_evidence(tmp_path):
    st = Store(tmp_path / "t.db")
    d = date(2026, 9, 22)
    synthetic_day(st, d, ["A", "B", "C", "D"])
    seed_closed_trades(st, d, 19)  # < 20 closed trades today
    cur = Params(entry_dev_pct=0.80)
    st.write_params(cur.to_dict(), "manual", NOW - timedelta(hours=1))
    summary = run_sweep(st, cur, d, NOW)
    assert summary["changed"] is False and summary["chosen"] == cur.to_dict()
    assert summary["reason"].startswith("insufficient_evidence")
    assert summary["best"] is not None  # the grid was still evaluated and recorded
    assert st.read_params()["source"] == "manual"  # untouched
    assert len(st.params_history(7, source="sweep")) == 1  # summary still written


def test_sweep_no_eligible_combination(tmp_path):
    st = Store(tmp_path / "t.db")
    d = date(2026, 9, 22)
    synthetic_day(st, d, ["A"], dip_every=200)  # ~1 trade a day → nothing eligible
    seed_closed_trades(st, d, 25)
    cur = Params()
    summary = run_sweep(st, cur, d, NOW)
    assert summary["eligible"] == 0 and summary["changed"] is False
    assert summary["reason"].startswith("no_eligible_combination")
    assert st.params_history(7, source="sweep")[0]["params"] == cur.to_dict()


def test_sweep_no_data(tmp_path):
    st = Store(tmp_path / "t.db")
    summary = run_sweep(st, Params(), date(2026, 9, 22), NOW)
    assert summary["reason"].startswith("no_data") and summary["combos_evaluated"] == 0
    assert len(st.params_history(7)) == 1


def test_eligibility_threshold_and_scores(tmp_path):
    st = Store(tmp_path / "t.db")
    d = date(2026, 9, 22)
    synthetic_day(st, d, ["A", "B"], dip_every=30)  # ~12 dips per symbol → ~24 trades/day total
    days = load_days(st, [d.isoformat()])
    results = evaluate_grid(days, Params())
    r_fire = next(r for r in results if r["params"] == {"entry_dev_pct": 0.30, "vol_mult": 1.50,
                                                         "max_hold_bars": 10, "stop_pct": 0.50})
    assert r_fire["trades"] >= 20 and r_fire["eligible"]
    r_none = next(r for r in results if r["params"]["entry_dev_pct"] == 0.80 and r["params"]["vol_mult"] == 2.0)
    assert r_none["trades"] == 0 and not r_none["eligible"]


def test_sweep_is_idempotent_per_date(tmp_path):
    st = Store(tmp_path / "t.db")
    d = date(2026, 9, 22)
    synthetic_day(st, d, ["A", "B", "C", "D"])
    seed_closed_trades(st, d, 25)
    cur = Params(entry_dev_pct=0.80)
    st.write_params(cur.to_dict(), "manual", NOW - timedelta(hours=1))
    first = run_sweep(st, cur, d, NOW)
    after_first = st.read_params()
    second = run_sweep(st, Params.from_dict(after_first["params"]), d, NOW + timedelta(minutes=33))
    assert second["reason"].startswith("already_swept") and second["changed"] is False
    assert second["combos_evaluated"] == 0
    assert st.read_params() == after_first                   # no second step
    assert len(st.params_history(7, source="sweep")) == 1    # no second row
    # the next session date sweeps normally
    assert not run_sweep(st, cur, date(2026, 9, 23), NOW + timedelta(days=1))["reason"].startswith("already_swept")
    assert first["reason"]  # first run did real work
