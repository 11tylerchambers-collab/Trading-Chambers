from datetime import datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.data import SymbolState
from chambers.store import Bar
from chambers.strategy import REASONS, Params, evaluate, hypothesis, position_qty

T0 = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
P = Params()


def state(closes, volumes, symbol="X"):
    """Flat h=l=c bars → vwap is the volume-weighted mean close."""
    s = SymbolState(symbol)
    s.update([Bar(T0 + timedelta(minutes=i), c, c, c, c, v) for i, (c, v) in enumerate(zip(closes, volumes))])
    return s


def base(n=20, price=100.0, vol=100.0):
    return [price] * (n - 1), [vol] * (n - 1)


def with_last(close, vol, n=20):
    c, v = base(n)
    return state(c + [close], v + [vol])


def test_all_reasons_are_reachable():
    seen = {}
    s = with_last(100, 100)
    seen["entries_closed"] = evaluate(s, P, False, entries_allowed=False).reason
    seen["insufficient_bars"] = evaluate(state([100] * 19, [100] * 19), P, False).reason
    seen["already_in_position"] = evaluate(with_last(99.0, 500), P, True).reason
    seen["no_position_slot"] = evaluate(with_last(99.0, 500), P, False, slots_available=False).reason
    seen["dev_too_small"] = evaluate(with_last(99.9, 500), P, False).reason
    seen["vol_too_low"] = evaluate(with_last(99.0, 100), P, False).reason
    seen["short_disabled"] = evaluate(with_last(101.0, 500), P.replace(allow_short=False), False).reason
    seen["fired"] = evaluate(with_last(99.0, 500), P, False).reason
    for r in REASONS:
        assert seen[r] == r, r


def test_long_fires_with_values():
    s = with_last(99.0, 500)          # 19 bars at 100 then a dip to 99 on 5x volume
    sig = evaluate(s, P, False)
    assert sig.fired and sig.side == "long" and sig.reason == "fired"
    assert sig.price == 99.0
    assert sig.vwap == pytest.approx((19 * 100 * 100 + 99 * 500) / (1900 + 500))
    assert sig.dev_pct == pytest.approx((99.0 - sig.vwap) / sig.vwap * 100)
    assert sig.dev_pct < -P.entry_dev_pct
    assert sig.vol_ratio == pytest.approx(500 / ((19 * 100 + 500) / 20))
    assert sig.vol_ratio >= P.vol_mult


def test_short_fires_and_short_disabled():
    s = with_last(101.0, 500)
    sig = evaluate(s, P, False)
    assert sig.fired and sig.side == "short" and sig.dev_pct > P.entry_dev_pct
    sig2 = evaluate(s, P.replace(allow_short=False), False)
    assert not sig2.fired and sig2.side is None and sig2.reason == "short_disabled"
    assert sig2.dev_pct == sig.dev_pct  # values still logged


def test_non_fires_carry_values():
    sig = evaluate(with_last(99.0, 100), P, False)
    assert sig.reason == "vol_too_low" and sig.fired is False and sig.side is None
    assert sig.price == 99.0 and sig.vwap is not None and sig.dev_pct is not None and sig.vol_ratio is not None
    closed = evaluate(with_last(99.0, 500), P, False, entries_allowed=False)
    assert closed.reason == "entries_closed" and closed.dev_pct is not None


def test_thresholds_are_inclusive():
    # build a state whose dev is exactly -entry_dev_pct and vol_ratio exactly vol_mult
    # 19 bars at 100 vol 100; last bar close c, vol v: vwap = (190000 + c*v)/(1900+v); ratio = v/((1900+v)/20)
    # choose v so ratio == 1.5 -> v = 1.5*(1900+v)/20 -> 20v = 2850 + 1.5v -> v = 154.054054...
    v = 2850 / 18.5
    # choose c so dev == -0.30%: c = vwap*(1-0.003); vwap = (190000 + c*v)/(1900+v) -> solve for c
    # c = (1-0.003)*(190000 + c v)/(1900+v)  =>  c(1900+v) = 0.997*190000 + 0.997 c v
    c = 0.997 * 190000 / ((1900 + v) - 0.997 * v)
    s = state([100.0] * 19 + [c], [100.0] * 19 + [v])
    sig = evaluate(s, P, False)
    assert sig.dev_pct == pytest.approx(-0.30, abs=1e-9) and sig.vol_ratio == pytest.approx(1.5, abs=1e-9)
    assert sig.fired and sig.side == "long"


def test_insufficient_bars_respects_min_bars_param():
    s = with_last(99.0, 500)  # 20 bars
    assert evaluate(s, P.replace(min_bars_before_entry=21), False).reason == "insufficient_bars"
    assert evaluate(s, P.replace(min_bars_before_entry=20), False).reason == "fired"


def test_reason_precedence():
    s = with_last(99.0, 500)  # would fire
    assert evaluate(s, P, True, slots_available=False).reason == "already_in_position"
    assert evaluate(s, P, True, entries_allowed=False).reason == "entries_closed"
    few = state([100] * 19, [100] * 19)
    assert evaluate(few, P, True).reason == "insufficient_bars"
    # dev check comes before vol check
    assert evaluate(with_last(99.9, 10), P, False).reason == "dev_too_small"
    # vol check comes before short_disabled
    assert evaluate(with_last(101.0, 100), P.replace(allow_short=False), False).reason == "vol_too_low"


def test_params_roundtrip_and_validation():
    p = Params.from_dict({"entry_dev_pct": "0.4", "allow_short": "false", "max_hold_bars": 7.0})
    assert p.entry_dev_pct == 0.4 and p.allow_short is False and p.max_hold_bars == 7
    assert Params.from_dict(p.to_dict()) == p
    with pytest.raises(ValueError):
        Params(entry_dev_pct=0).validate()
    with pytest.raises(ValueError):
        Params(max_hold_bars=0).validate()
    Params().validate()


def test_position_qty_and_hypothesis():
    assert position_qty(100.0, P) == 20
    assert position_qty(2001.0, P) == 1
    assert position_qty(1999.0, P) == 1
    assert position_qty(33.33, Params(notional_per_trade=100)) == 3
    sig = evaluate(with_last(99.0, 500), P, False)
    h = hypothesis(sig, P)
    assert h["expect"] == "return to vwap" and h["expect_within_bars"] == 10
    assert h["expect_move_pct"] == pytest.approx(abs(sig.dev_pct), abs=1e-4) and h["dev_pct"] < 0
