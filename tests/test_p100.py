"""The $100 cash-account replay (PHASE1A §8): T+1, inverse ETFs, shadow shorts, min order, carry-forward."""
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.p100 import (P100_BUFFER, Prices, SigTrade, next_business_day, run_p100, simulate, start_state,
                           translate)
from chambers.store import Bar, Store

from .labdata import fill_store

D = date(2026, 9, 24)          # a Thursday
T = lambda h, m: datetime(2026, 9, 24, h, m, 5, tzinfo=ET)
FRESH = {"equity": 100.0, "settled": 100.0, "unsettled": []}


def sig(sym, side, eh, em, xh, xm, e=100.0, x=101.0, stop=0.005, src="S0"):
    return SigTrade(src, sym, side, T(eh, em), e, T(xh, xm), x, "vwap_touch", stop)


def prices(**series):
    out = {}
    for sym, pts in series.items():
        out[sym] = [Bar(datetime(2026, 9, 24, h, m, tzinfo=ET), c, c, c, c, 1) for (h, m), c in pts]
    return Prices(out)


def test_sale_proceeds_unusable_same_day_then_settle_t_plus_1():
    # 1% of $100 over a 0.5% stop wants $200 of AAPL: capped by the $100 settled cash
    sigs = [sig("AAPL", "long", 10, 0, 10, 30), sig("MSFT", "long", 11, 0, 11, 30)]
    r = simulate(D, sigs, prices(), FRESH, next_business_day(D))
    L = r.ledger
    assert L["trades"] == 1 and L["detail"]["skipped"] == {"no_settled_cash": 1}   # MSFT: AAPL's sale not settled
    t = r.trades[0]
    assert t["symbol"] == "AAPL" and t["qty"] == pytest.approx(1.0) and L["settled_cash_end"] == pytest.approx(0.0)
    assert L["unsettled"][0][1] == "2026-09-25"                                    # Friday, T+1
    proceeds = L["unsettled"][0][0]
    assert proceeds == pytest.approx(100 + t["net_pnl"]) and L["end_equity"] == pytest.approx(proceeds)
    # Friday: yesterday's proceeds have settled and are usable
    fri = date(2026, 9, 25)
    r2 = simulate(fri, [SigTrade("S0", "MSFT", "long", datetime(2026, 9, 25, 10, 0, 5, tzinfo=ET), 50.0,
                                 datetime(2026, 9, 25, 10, 30, 5, tzinfo=ET), 50.5, "vwap_touch", 0.005)],
                  prices(), {"equity": L["end_equity"], "settled": L["settled_cash_end"], "unsettled": L["unsettled"]},
                  next_business_day(fri))
    assert r2.ledger["settled_cash_start"] == pytest.approx(proceeds) and r2.ledger["trades"] == 1
    assert r2.ledger["unsettled"][0][1] == "2026-09-28"                           # Friday's sale settles Monday


def test_min_order_and_fractional():
    sigs = [sig("AAPL", "long", 10, 0, 10, 30, e=333.33, x=334.0)]
    r = simulate(D, sigs, prices(), {"equity": 100.0, "settled": 0.6, "unsettled": [[99.4, "2026-09-25"]]},
                 next_business_day(D))
    assert r.ledger["trades"] == 0 and r.ledger["detail"]["skipped"] == {"no_settled_cash": 1}
    r = simulate(D, sigs, prices(), FRESH, next_business_day(D))
    assert r.trades[0]["qty"] == pytest.approx(0.3, abs=1e-3) and r.trades[0]["qty"] != int(r.trades[0]["qty"])


def test_inverse_etf_translation_and_shadow_shorts():
    p = prices(SH=[((9, 59), 40.0), ((10, 29), 39.6)], PSQ=[((10, 59), 20.0), ((11, 29), 20.3)])
    sigs = [sig("SPY", "short", 10, 0, 10, 30, e=500, x=505),       # SPY rose: the short (and SH) lose
            sig("QQQ", "short", 11, 0, 11, 30, e=400, x=394),       # QQQ fell: PSQ gains
            sig("AAPL", "short", 12, 0, 12, 30, e=200, x=198)]      # no inverse ETF: shadow only
    spec, _ = translate(sigs[0], p)
    assert spec == {"symbol": "SH", "entry": 40.0, "exit": 39.6}
    assert translate(sigs[2], p) == (None, "short_not_translatable")
    r = simulate(D, sigs, p, FRESH, next_business_day(D))
    real = [t for t in r.trades if not t["learning_only"]]
    shadow = [t for t in r.trades if t["learning_only"]]
    assert [(t["symbol"], t["signal_symbol"], t["side"]) for t in real] == [("SH", "SPY", "long")]   # PSQ: no cash
    assert r.ledger["detail"]["skipped"] == {"no_settled_cash": 1, "short_not_translatable": 1}
    assert sorted(t["symbol"] for t in shadow) == ["AAPL", "QQQ", "SPY"] and all(t["side"] == "short" for t in shadow)
    # shadow shorts are reported but never counted in equity
    assert r.ledger["shadow_trades"] == 3 and r.ledger["shadow_net"] != 0
    assert r.ledger["end_equity"] == pytest.approx(100.0 + real[0]["net_pnl"])
    assert r.ledger["net_pnl"] == pytest.approx(real[0]["net_pnl"])
    cost = real[0]["est_cost"]
    q = real[0]["qty"]
    assert cost > P100_BUFFER * (40.0 + 39.6) * q                  # buffer on top of the replay cost model


def test_missing_inverse_bars_are_skipped():
    r = simulate(D, [sig("SPY", "short", 10, 0, 10, 30)], prices(), FRESH, next_business_day(D))
    assert r.ledger["detail"]["skipped"] == {"no_inverse_data": 1} and r.ledger["shadow_trades"] == 1


def test_run_p100_full_days_carry_forward_and_own_params(tmp_path):
    st = Store(tmp_path / "t.db")
    days = fill_store(st, sessions=8, s0_symbols=("AAPL", "SPY", "QQQ"), extra=("SH", "PSQ"))
    st.write_params({"entry_dev_pct": 0.3, "vol_mult": 1.5}, "sweep", datetime(2026, 9, 20, tzinfo=ET), "S0")
    now = datetime(2026, 9, 25, 17, 0, tzinfo=ET)
    r1 = run_p100(st, days[-2], now, ["AAPL", "SPY", "QQQ"], tune=False)
    assert r1.ledger["start_equity"] == 100.0 and st.p100_ledger_for(days[-2]) is not None
    r2 = run_p100(st, days[-1], now, ["AAPL", "SPY", "QQQ"], tune=True)
    assert r2.ledger["start_equity"] == pytest.approx(r1.ledger["end_equity"])
    assert r2.ledger["settled_cash_start"] == pytest.approx(r1.ledger["settled_cash_end"] +
                                                            sum(a for a, _ in r1.ledger["unsettled"]))
    rows = st.p100_trades_for(days[-1])
    assert len(rows) == r2.ledger["trades"] + r2.ledger["shadow_trades"]
    assert all(r["news_day"] in (0, 1) for r in rows)
    # re-running a day replaces it rather than duplicating
    run_p100(st, days[-1], now, ["AAPL", "SPY", "QQQ"], tune=False)
    assert len(st.p100_trades_for(days[-1])) == len(rows) and len(st.p100_ledger(10)) == 2
    # P100 keeps its own params under profile P100; the live S0 row is untouched
    assert st.read_params("S0", "P100") is not None and st.read_params("S1", "P100") is not None
    assert st.read_params("S0", "LIVE")["params"] == {"entry_dev_pct": 0.3, "vol_mult": 1.5}
    assert st.has_sweep_for(days[-1], "S0", "P100") and not st.has_sweep_for(days[-1], "S0", "LIVE")
