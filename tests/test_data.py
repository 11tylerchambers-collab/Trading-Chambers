from datetime import datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.data import DataState, SymbolState
from chambers.store import Bar

T0 = datetime(2026, 9, 22, 9, 30, tzinfo=ET)


def mk(i, o, h, l, c, v):
    return Bar(T0 + timedelta(minutes=i), o, h, l, c, v)


# Hand-computed 5-bar example.
#   bar  h     l     c     v    typical=(h+l+c)/3   tp*v
#   0   101    99   100   100   100.0             10000
#   1   102   100   101   200   101.0             20200
#   2   103    99   102   100   101.3333          10133.33
#   3   101    98   100   300    99.6667          29900
#   4   100    97    98   300    98.3333          29500
#   Σv = 1000, Σ(tp*v) = 99733.33  → vwap = 99.73333
FIVE = [mk(0, 100, 101, 99, 100, 100), mk(1, 100, 102, 100, 101, 200), mk(2, 101, 103, 99, 102, 100),
        mk(3, 102, 101, 98, 100, 300), mk(4, 100, 100, 97, 98, 300)]


def test_vwap_hand_computed():
    s = SymbolState("X")
    s.update(FIVE[:1])
    assert s.vwap == pytest.approx(100.0)
    s.update(FIVE[1:2])
    assert s.vwap == pytest.approx((10000 + 20200) / 300)
    s.update(FIVE[2:])
    assert s.vwap == pytest.approx(99733.3333 / 1000, rel=1e-6)
    assert s.last_close == 98 and s.last_ts == T0 + timedelta(minutes=4) and s.bar_count == 5
    assert s.dev_pct == pytest.approx((98 - 99.73333) / 99.73333 * 100, rel=1e-4)


def test_avg_volume_20_none_until_20_then_mean_of_last_20():
    s = SymbolState("X")
    s.update([mk(i, 1, 1, 1, 1, 100 + i) for i in range(19)])
    assert s.avg_volume_20 is None and s.vol_ratio is None
    s.update([mk(19, 1, 1, 1, 1, 119)])
    assert s.avg_volume_20 == pytest.approx(sum(range(100, 120)) / 20)
    s.update([mk(20, 1, 1, 1, 1, 1000)])
    assert s.avg_volume_20 == pytest.approx((sum(range(101, 120)) + 1000) / 20)
    assert s.vol_ratio == pytest.approx(1000 / s.avg_volume_20)


def test_update_is_idempotent_and_ordered():
    s = SymbolState("X")
    added = s.update(FIVE)
    assert len(added) == 5
    assert s.update(FIVE) == []                 # exact repeat adds nothing
    assert s.update(FIVE[2:4]) == []            # older bars ignored
    vwap_before = s.vwap
    assert s.update(list(reversed(FIVE))) == [] and s.vwap == vwap_before
    # out-of-order new bars are sorted before appending
    new = [mk(6, 1, 1, 1, 1, 10), mk(5, 1, 1, 1, 1, 10)]
    assert [b.ts for b in s.update(new)] == [T0 + timedelta(minutes=5), T0 + timedelta(minutes=6)]
    assert s.bar_count == 7


def test_empty_state():
    s = SymbolState("X")
    assert s.vwap is None and s.last_close is None and s.last_ts is None and s.dev_pct is None


def test_zero_volume_bars_do_not_break_vwap():
    s = SymbolState("X")
    s.update([mk(0, 10, 10, 10, 10, 0)])
    assert s.vwap is None  # no volume yet, no vwap
    s.update([mk(1, 10, 12, 8, 10, 100)])
    assert s.vwap == pytest.approx(10.0)


def test_data_state_update_many_and_last_ts():
    d = DataState(["A", "B"])
    d.update_many({"A": FIVE, "B": FIVE[:2]})
    assert d["A"].bar_count == 5 and d["B"].bar_count == 2
    assert d.last_ts() == T0 + timedelta(minutes=1)  # min across symbols
    d.update("C", FIVE[:1])                          # unknown symbol is added
    assert "C" in d.symbols
    d.reset()
    assert d["A"].bar_count == 0 and d.last_ts() is None
