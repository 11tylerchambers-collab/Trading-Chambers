"""Night lab: candidates, train/grade split, suggestion rule, pausing, one full run (PHASE1A §7)."""
from datetime import date, datetime, timedelta

import pytest

import chambers.lab as lab
from chambers.clock import ET
from chambers.engine import Position
from chambers.lab import L1, L2, Pauser, l1_entry, l1_exit, l2_entry, l2_exit, meets_rule, run_lab, split
from chambers.store import Bar, Store

from .labdata import fill_store

D = date(2026, 9, 22)
O = datetime(2026, 9, 22, 9, 30, tzinfo=ET)


def five(closes, start=O, hi=None, lo=None):
    return [Bar(start + timedelta(minutes=5 * i), c, (hi or {}).get(i, c + 0.05), (lo or {}).get(i, c - 0.05), c, 100)
            for i, c in enumerate(closes)]


def test_l1_opening_range_breakout_branches():
    p = L1.params()                                   # range 9:30-10:30 (12 five-minute bars), target 2R
    rng = [100 + (0.5 if i % 2 else -0.5) for i in range(12)]   # high 100.55, low 99.45
    assert l1_entry(five(rng[:6]), p).reason == "insufficient_bars"
    assert l1_entry(five(rng + [100.2]), p).reason == "inside_range"
    ev = l1_entry(five(rng + [101.0]), p)
    # stop = the closer of the range low (99.45) and entry − height (101 − 1.1 = 99.9) → 99.9
    assert ev.fired and ev.side == "long" and ev.stop_distance == pytest.approx(1.1)
    ev = l1_entry(five(rng + [99.0]), p)
    assert ev.side == "short" and ev.stop_distance == pytest.approx(1.1)
    # only the first close outside the range counts
    assert l1_entry(five(rng + [101.0, 100.2, 101.2]), p).reason == "already_broke_out"
    pos = Position(1, "SPY", "long", 10, 101.0, O, None, stop_price=99.9)
    assert l1_exit(pos, five(rng + [101.0, 101.5]), p) is None
    assert l1_exit(pos, five(rng + [101.0, 103.2]), p) == "target"            # 101 + 2 × 1.1
    assert l1_exit(pos, five(rng + [101.0, 99.8]), p) == "stop_loss"


def daily(closes):
    return [Bar(datetime(2026, 1, 1, tzinfo=ET) + timedelta(days=i), c, c + 1, c - 1, c, 1e6) for i, c in enumerate(closes)]


def test_l2_pullback_branches():
    p = L2.params()
    up = [100 + 0.5 * i for i in range(210)]
    assert l2_entry(daily(up[:150]), p).reason == "insufficient_bars"
    assert l2_entry(daily(up), p).reason == "no_pullback"
    pb = up + [up[-1] - 1, up[-1] - 2, up[-1] - 3]
    ev = l2_entry(daily(pb), p)
    assert ev.fired and ev.side == "long" and ev.detail["sma200"] < pb[-1]
    assert l2_entry(daily([200 - 0.5 * i for i in range(210)] + [90, 89, 88]), p).reason == "below_sma200"
    pos = Position(1, "SPY", "long", 10, pb[-1], None, None, stop_price=pb[-1] - 10, bars_held=1)
    assert l2_exit(pos, daily(pb + [pb[-1] + 0.5]), p) == "up_close"
    pos.bars_held = 5
    assert l2_exit(pos, daily(pb + [pb[-1] - 0.5]), p) == "time_stop"
    pos.bars_held = 1
    assert l2_exit(pos, daily(pb + [pb[-1] - 11]), p) == "stop_loss"


def test_split_and_suggestion_rule():
    tr, gr = split(list(range(25)))
    assert tr == list(range(12)) and gr == list(range(12, 25))          # graded half is the later, unseen part
    ok = {"grade_net": 50.0, "twin_grade_net": 10.0}
    assert meets_rule(ok, 20)[0] is True
    assert meets_rule(ok, 19) == (False, "insufficient_sessions: 19 < 20")
    assert meets_rule({"grade_net": -1.0, "twin_grade_net": -5.0}, 30)[0] is False
    assert "does not beat its twin" in meets_rule({"grade_net": 5.0, "twin_grade_net": 8.0}, 30)[1]


def test_pauses_while_engine_cycles_are_slow(tmp_path):
    st = Store(tmp_path / "t.db")
    clock = {"now": datetime(2026, 9, 22, 16, 45, tzinfo=ET)}
    st.write_cycle(clock["now"] - timedelta(seconds=30), "running", 20, 0, 0, 0, 25000, "S0")

    def sleep(s):
        clock["now"] += timedelta(seconds=s)

    p = Pauser(st, lambda: clock["now"], sleep)
    p.check()
    assert p.paused_s == 120.0                        # until the slow cycle is > 2 minutes old
    p.check()
    assert p.paused_s == 120.0


def test_one_full_lab_run_writes_only_lab_tables(tmp_path, monkeypatch):
    # a 6-combination S0 grid keeps this test fast; `python -m chambers.main --lab` runs the full 288
    full = lab.s0_grid_combos
    monkeypatch.setattr(lab, "s0_grid_combos", lambda base: full(base)[::48])
    st = Store(tmp_path / "t.db")
    fill_store(st, sessions=24, s0_symbols=("AAPL", "SPY", "QQQ"), extra=())
    other = {t: st._one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("bars", "bars_tf", "trades", "params", "signals")}
    now = datetime(2026, 9, 25, 17, 30, tzinfo=ET)
    out = run_lab(st, now, ["AAPL", "SPY", "QQQ"], sleep_fn=lambda s: None, now_fn=lambda: now)
    assert [r["candidate"] for r in out["results"]] == ["L1_orb", "L2_pullback", "L3_vwap_idx", "L3_vwap_news"]
    for r in out["results"]:
        assert r["sessions"] == 24 and len(r["train_dates"]) == 12 and len(r["grade_dates"]) == 12
        assert max(r["train_dates"]) < min(r["grade_dates"])
        assert r["meets_rule"] == meets_rule(r, 24)[0]
    assert out["results"][3]["params"]["skip_news_days"] is True
    res = st.lab_results()
    assert len(res) == 4 and st.lab_runs(1)[0]["status"] == "done"
    sug = st.lab_suggestions()
    assert [s["candidate"] for s in sug][::-1] == out["suggestions"]
    assert all(s["status"] == "pending" for s in sug)
    after = {t: st._one(f"SELECT COUNT(*) FROM {t}")[0] for t in other}
    assert after == other                                                  # nothing outside the lab tables


def test_launch_uses_nice_and_a_separate_process(monkeypatch):
    seen = {}

    class P:
        pid = 4321

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return P()

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert lab.launch_lab("/usr/bin/python3", "/opt/chambers") == 4321
    assert seen["args"] == ["/usr/bin/python3", "-m", "chambers.main", "--lab"]
    assert seen["kw"]["cwd"] == "/opt/chambers" and seen["kw"]["start_new_session"] is True
    assert callable(seen["kw"]["preexec_fn"])
