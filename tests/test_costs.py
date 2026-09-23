"""Live trade costing (fill vs quote mid), the --recompute-costs backfill and --replay --params. No network."""
from datetime import datetime

import pytest

from chambers.clock import ET
from chambers.engine import fill_vs_mid, live_trade_economics, trade_economics
from chambers.main import apply_param_overrides, main, recompute_costs
from chambers.store import Store
from chambers.strategy import Params

T0 = datetime(2026, 9, 23, 10, 0, 5, tzinfo=ET)
T1 = datetime(2026, 9, 23, 10, 10, 5, tzinfo=ET)


def test_fill_vs_mid_is_absolute_and_ignores_missing_or_crossed_quotes():
    assert fill_vs_mid(100.05, 99.9, 100.1) == pytest.approx(0.05)
    assert fill_vs_mid(99.95, 99.9, 100.1) == pytest.approx(0.05)   # better than mid still counts
    assert fill_vs_mid(100.0, None, 100.1) == 0.0
    assert fill_vs_mid(100.0, 100.2, 100.1) == 0.0                   # crossed


def test_live_economics_long_and_short():
    # long 10 @ 100.02 (mid 100.00), out @ 100.97 (mid 101.00)
    gross, cost, net = live_trade_economics("long", 10, 100.02, 100.97, 99.99, 100.01, 100.99, 101.01)
    slippage = 0.0001 * 100.02 * 10 * 2
    assert gross == pytest.approx(9.5)
    assert cost == pytest.approx((0.02 + 0.03) * 10 + slippage)
    assert net == pytest.approx(gross - slippage)          # fill-vs-mid is already inside gross
    gross_s, cost_s, net_s = live_trade_economics("short", 10, 100.0, 101.0, None, None, None, None)
    assert gross_s == pytest.approx(-10.0) and cost_s == pytest.approx(0.2) and net_s == pytest.approx(-10.2)


def test_live_economics_wide_stale_quote_does_not_move_net():
    # a 5% wide IEX quote inflates the recorded cost, but net depends only on fills + slippage
    _, cost_tight, net_tight = live_trade_economics("long", 10, 100.0, 101.0, 99.99, 100.01, 100.99, 101.01)
    _, cost_wide, net_wide = live_trade_economics("long", 10, 100.0, 101.0, 97.0, 102.0, 98.0, 103.0)
    assert cost_wide > cost_tight
    assert net_wide == pytest.approx(net_tight)


def test_replay_costing_is_unchanged():
    gross, cost, net = trade_economics("long", 10, 100.0, 101.0, 99.99, 100.01, 100.99, 101.01)
    assert cost == pytest.approx((0.01 + 0.01) * 10 + 0.0001 * 1000 * 2) and net == pytest.approx(gross - cost)


def _closed_trade(st, side, qty, entry, exit_, eb, ea, xb, xa, old_cost, old_net):
    tid = st.open_trade("AAPL", side, qty, T0, entry, "o1", eb, ea, {}, {})
    st.close_trade(tid, T1, exit_, "o2", xb, xa, "time_stop", 10, -0.1, 0.2, 0.0, old_cost, old_net)
    return tid


def test_recompute_costs_rewrites_closed_trades_only(tmp_path):
    st = Store(tmp_path / "t.db")
    a = _closed_trade(st, "long", 10, 100.02, 100.97, 99.99, 100.01, 100.99, 101.01, 99.0, -99.0)
    b = _closed_trade(st, "short", 5, 50.0, 49.0, 49.0, 51.0, None, None, 99.0, -99.0)
    still_open = st.open_trade("MSFT", "long", 3, T0, 10.0, "o3", 9.9, 10.1, {}, {})
    n, old, new = recompute_costs(st)
    assert n == 2 and old == pytest.approx(-198.0)
    ta, tb = st.get_trade(a), st.get_trade(b)
    assert ta.gross_pnl == pytest.approx(9.5) and ta.net_pnl == pytest.approx(9.5 - 0.0001 * 100.02 * 10 * 2)
    assert ta.est_cost == pytest.approx(0.5 + 0.0001 * 100.02 * 10 * 2)
    assert tb.gross_pnl == pytest.approx(5.0) and tb.est_cost == pytest.approx(0.0 + 0.05)  # entry fill = mid
    assert new == pytest.approx(ta.net_pnl + tb.net_pnl)
    assert st.get_trade(still_open).net_pnl is None
    # idempotent
    assert recompute_costs(st)[2] == pytest.approx(new)


def test_apply_param_overrides():
    base = Params(entry_dev_pct=0.40, vol_mult=1.25, stop_pct=0.75)
    p = apply_param_overrides(base, "entry_dev_pct=0.30, vol_mult=1.50,max_hold_bars=10,stop_pct=0.50")
    assert (p.entry_dev_pct, p.vol_mult, p.max_hold_bars, p.stop_pct) == (0.30, 1.50, 10, 0.50)
    assert isinstance(p.max_hold_bars, int) and p.reentry_cooldown_bars == base.reentry_cooldown_bars
    assert apply_param_overrides(base, "allow_short=false").allow_short is False
    assert apply_param_overrides(base, None) is base
    with pytest.raises(ValueError):
        apply_param_overrides(base, "entry_dev=0.3")
    with pytest.raises(ValueError):
        apply_param_overrides(base, "entry_dev_pct")


def test_cli_rejects_bad_params_usage():
    with pytest.raises(SystemExit):
        main(["--params", "entry_dev_pct=0.3"])                        # without --replay
    with pytest.raises(SystemExit):
        main(["--replay", "2026-09-23", "--params", "nope=1"])        # unknown key, before any DB access


def test_cli_replay_uses_params_override(tmp_path, monkeypatch, capsys):
    import chambers.main as m
    st = Store(tmp_path / "t.db")
    st.write_params(Params(entry_dev_pct=0.40).to_dict(), "sweep", T0)
    monkeypatch.setattr(m, "optional_runtime", lambda: (st, None, None))
    assert main(["--replay", "2026-09-23", "--params", "entry_dev_pct=0.30,vol_mult=1.5"]) == 0
    out = capsys.readouterr().out
    assert "'entry_dev_pct': 0.3" in out and "'vol_mult': 1.5" in out and "trades=0" in out
