"""The whole Phase 1A engine wired as the service wires it (runtime.build_engine), on the mock broker."""
import math
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from chambers.clock import ET
from chambers.runtime import build_engine, build_jobs
from chambers.store import Bar, Store

from .mocks import UNIVERSE, FakeTime, MockBroker, flat_bars, make_clock

D = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
UTC = timezone.utc


def config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def world(tmp_path, now):
    ft = FakeTime(now)
    broker = MockBroker(sessions=[(d, datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                                   datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))
                                  for d in (date(2026, 9, 21), D, date(2026, 9, 23))])
    for s in UNIVERSE + ["GLD", "USO"]:
        broker.bars[s] = [Bar(OPEN + timedelta(minutes=i), 100 + 0.05 * math.sin(i + len(s)),
                              100.1, 99.9, 100 + 0.05 * math.sin(i + len(s)), 100.0) for i in range(390)]
    start = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
    broker.tf_bars[("BTC/USD", 60)] = [Bar(start + timedelta(hours=i), 1e5, 1e5 + 50, 1e5 - 50, 1e5 + 10 * math.sin(i), 5)
                                       for i in range(24 * 6)]
    st = Store(tmp_path / "t.db")
    clk = make_clock(ft, broker)
    sched, runners = build_engine(config(), st, broker, clk, sleep_fn=ft.sleep, fetch_history=False)
    return sched, runners, st, broker, ft


def test_full_day_all_sleeves_one_process(tmp_path):
    sched, runners, st, broker, ft = world(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET))
    assert list(runners) == ["S0", "S1", "S2", "S3"]
    assert [s["id"] for s in st.sleeves()] == ["S0", "S1", "S2", "S3"] and st.sleeves()[1]["capital"] == 20000
    sched.jobs = build_jobs(config(), st, broker, sched.clock, runners, backup_dir=tmp_path / "backups")
    sched.startup()
    while ft.now < datetime(2026, 9, 22, 21, 0, tzinfo=ET):
        sched.tick()
    assert len(st.cycles_for_day(D, "S0")) == 385
    assert len(st.cycles_for_day(D, "S1")) == 25
    assert [c["ts"][11:19] for c in st.cycles_for_day(D, "S3")] == ["13:30:05", "15:55:00"]
    s2 = st._query("SELECT ts FROM cycles WHERE sleeve_id='S2' ORDER BY id")
    assert len(s2) == 12                                        # 09:00:10 .. 20:00:10 ET, every hour
    for sid in ("S0", "S1", "S3"):
        assert st.has_sweep_for(D, sid), sid
    hb = st.read_heartbeats()
    assert set(hb) >= {"ALL", "S0", "S1", "S2", "S3"}
    assert st.errors_count(where_like="unhandled%") == 0
    # the nightly chain ran after the sweeps: recon (baseline on a new db) and the backup
    # (S2's startup catch-up sweep of the 09-21 UTC day ran one too)
    assert [r["kind"] for r in st.recon_for_day(D)] == ["crypto_rollover", "equity_session", "crypto_rollover"]
    assert (tmp_path / "backups" / "chambers-2026-09-22.db").exists()
    # S2's 00:30 UTC rollover (20:30 ET) swept the UTC day and ran the crypto recon
    assert st.has_sweep_for("2026-09-22", "S2") and st.last_recon()["kind"] == "crypto_rollover"
    seeds = st._query("SELECT sleeve_id, day FROM twin_seeds ORDER BY sleeve_id")
    assert {r["sleeve_id"] for r in seeds} == {"S0", "S1", "S2", "S3"}


def test_once_each_sleeve_after_hours(tmp_path):
    """What `--once --sleeve X` does: start every sleeve, reconcile them together, run one cycle of X."""
    sched, runners, st, broker, ft = world(tmp_path, datetime(2026, 9, 22, 18, 0, 5, tzinfo=ET))
    broker.positions_ = {"GLD": 10}                              # an S3 overnight position
    st.open_trade("GLD", "long", 10, datetime(2026, 9, 21, 13, 30, 5, tzinfo=ET), 100.0, "g1", None, None,
                  {"stop_price": 90.0, "entry_bar_ts": "2026-09-21T09:30:00-04:00"}, {}, sleeve_id="S3")
    sched.startup()
    assert broker.positions_ == {"GLD": 10} and "GLD" in runners["S3"].positions   # not an orphan
    for sid, r in runners.items():
        res = r.run_cycle()
        assert res.cycle_id is not None and res.errors == 0, (sid, res)
    s0 = st.signals_for_day(D, "S0")
    assert len(s0) == 20 and all(s["reason"] == "entries_closed" for s in s0[-20:])   # S0 unchanged in --once
    assert {s["reason"] for s in st.signals_for_day(D, "S1")} <= {"entries_closed", "insufficient_bars"}


def test_gate_1a_over_a_simulated_day(tmp_path):
    from chambers.gate1a import format_gate_1a, run_gate_1a
    empty = run_gate_1a(Store(tmp_path / "e.db"))
    assert not any(i["pass"] for i in empty["items"])
    sched, runners, st, broker, ft = world(tmp_path, datetime(2026, 9, 21, 23, 55, tzinfo=ET))
    sched.startup()
    while ft.now < datetime(2026, 9, 22, 23, 55, tzinfo=ET):
        sched.tick()
    res = run_gate_1a(st)
    items = {i["id"]: i for i in res["items"]}
    assert res["sessions"] == ["2026-09-22"]
    assert items[1]["pass"], items[1]["detail"]          # S0 385/385, S1 25/25, S3 2/2, S2 24/24
    assert items[2]["pass"], items[2]["detail"]          # every expected cycle logged its evaluations
    assert "closed trades/day (not graded)" in items[2]["detail"]
    assert items[3]["pass"] and items[7]["pass"]
    assert not items[6]["pass"]                           # no Telegram
    assert "OVERALL: FAIL" in format_gate_1a(res)


def test_broker_check_text():
    from chambers.main import broker_check_text
    t = broker_check_text({"multiplier": "4", "pattern_day_trader": False, "daytrade_count": 2,
                           "intraday_adjustments": "0", "status": "ACTIVE"})
    assert "margin account, 4x" in t and "pattern day trader flag: False" in t and "day-trade count" in t
    assert "intraday_adjustments: 0" in t


def logged_day(st, minutes, with_reason=lambda m: True, s0_trades=0):
    """An S0-only day: `minutes` cycles from 9:30:05, each with one signals row (reason empty unless with_reason(m))."""
    open_ = datetime(2026, 9, 22, 9, 30, 5, tzinfo=ET)
    for m in range(minutes):
        t = open_ + timedelta(minutes=m)
        cid = st.write_cycle(t, "running", 1, 0, 0, 0, 1, "S0")
        st.write_signals(cid, [{"ts": t, "symbol": "SPY", "close": 100.0, "side": None, "fired": False,
                                "reason": "dev_too_small" if with_reason(m) else "", "params": {}}], "S0")
    for i in range(s0_trades):
        tid = st.open_trade("SPY", "long", 1, open_, 100.0, f"o{i}", None, None, {}, {}, sleeve_id="S0")
        st.close_trade(tid, open_ + timedelta(minutes=5), 100.0, f"x{i}", None, None, "time_stop", 5, 0.0, 0.0,
                       0.0, 0.01, -0.01)


@pytest.mark.parametrize("missing, ok", [(7, True), (8, False)])
def test_gate_1a_item2_grades_evaluation_logging_not_trades(tmp_path, missing, ok):
    from chambers.gate1a import run_gate_1a
    st = Store(tmp_path / "t.db")
    # 385 expected S0 cycles; 98% needs 378 with a reasoned signals row. Zero trades all day.
    logged_day(st, 385, with_reason=lambda m: m >= missing)
    item = {i["id"]: i for i in run_gate_1a(st, sleeves=("S0",))["items"]}[2]
    assert item["pass"] is ok, item["detail"]
    assert item["name"] == "each active sleeve wrote a signal row with a reason for >= 98% of its expected cycles"
    assert "S0 [0]" in item["detail"]


def test_gate_1a_item2_missing_cycles_count_as_unlogged(tmp_path):
    from chambers.gate1a import run_gate_1a
    st = Store(tmp_path / "t.db")
    logged_day(st, 300, s0_trades=3)                     # cycles stop at 14:30: 300/385
    items = {i["id"]: i for i in run_gate_1a(st, sleeves=("S0",))["items"]}
    assert not items[1]["pass"] and not items[2]["pass"]
    assert "77.9%" in items[2]["detail"] and "S0 [3]" in items[2]["detail"]
