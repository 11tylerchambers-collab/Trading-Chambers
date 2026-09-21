from datetime import date, datetime, timedelta

from chambers.clock import ET, Session
from chambers.gate import format_gate, run_gate
from chambers.store import Store


def fill_day(st: Store, d: date, minutes_ok=True, trades=35, complete=True, signals_ok=True, swept=True, unhandled=0):
    o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    sess = Session(d, o, o + timedelta(hours=6, minutes=30))
    mins = sess.cycle_minutes()
    if not minutes_ok:
        mins = mins[: int(len(mins) * 0.9)]
    for m in mins:
        cid = st.write_cycle(m + timedelta(seconds=5), "running", 20, 0, 0, 0, 10)
        n = 20 if signals_ok else 15
        st.write_signals(cid, [{"ts": m + timedelta(seconds=5), "symbol": f"S{i}", "reason": "dev_too_small",
                                "params": {}} for i in range(n)])
    for i in range(trades):
        tid = st.open_trade("AAPL", "long", 1, o + timedelta(minutes=10 + i), 100.0, None, None, None,
                            {"expect": "return to vwap"} if complete else None, {})
        st.close_trade(tid, o + timedelta(minutes=12 + i), 100.1, None, None, None, "vwap_touch", 2, -0.1, 0.1,
                       0.1, 0.02, 0.08 if complete else None)
    if swept:
        st.write_params_history(d, {}, "sweep", {"reason": "x"})
    for _ in range(unhandled):
        st.log_error("unhandled", "boom", None, o + timedelta(hours=1))


def test_gate_passes_on_five_good_days(tmp_path):
    st = Store(tmp_path / "g.db")
    days = [date(2026, 9, 14) + timedelta(days=i) for i in range(5)]
    for d in days:
        fill_day(st, d)
    res = run_gate(st)
    assert res["sessions"] == [d.isoformat() for d in days]
    assert all(it["pass"] for it in res["items"]) and len(res["items"]) == 6
    txt = format_gate(res)
    assert "OVERALL: PASS" in txt and txt.count("[PASS]") == 6


def test_gate_fails_each_item_independently(tmp_path):
    st = Store(tmp_path / "g.db")
    fill_day(st, date(2026, 9, 14), minutes_ok=False)
    fill_day(st, date(2026, 9, 15), trades=10)
    fill_day(st, date(2026, 9, 16), complete=False)
    fill_day(st, date(2026, 9, 17), signals_ok=False)
    fill_day(st, date(2026, 9, 18), swept=False, unhandled=1)
    res = run_gate(st)
    fails = {it["id"] for it in res["items"] if not it["pass"]}
    assert fails == {1, 2, 3, 4, 5, 6}
    d = {it["id"]: it["detail"] for it in res["items"]}
    assert "2026-09-14" in d[1] and "FAIL" in d[1].split("2026-09-14")[1].split(";")[0]
    assert "2026-09-15: 10 closed FAIL" in d[2]
    assert "OVERALL: FAIL" in format_gate(res)


def test_gate_uses_last_five_sessions_and_early_close(tmp_path):
    st = Store(tmp_path / "g.db")
    for i in range(7):
        fill_day(st, date(2026, 9, 7) + timedelta(days=i))
    res = run_gate(st)
    assert len(res["sessions"]) == 5 and res["sessions"][0] == "2026-09-09"
    # an early-close session shrinks the minute denominator; supplying it makes coverage exact
    d = date(2026, 9, 13)
    o = datetime(2026, 9, 13, 9, 30, tzinfo=ET)
    early = Session(d, o, datetime(2026, 9, 13, 13, 0, tzinfo=ET))
    res2 = run_gate(st, sessions={d: early})
    per = {p["date"]: p for p in res2["per_day"]}
    assert per["2026-09-13"]["minutes"] == len(early.cycle_minutes()) and per["2026-09-13"]["cycle_coverage"] == 1.0


def test_gate_empty_db(tmp_path):
    res = run_gate(Store(tmp_path / "g.db"))
    assert res["sessions"] == [] and all(not it["pass"] for it in res["items"])
    assert "only 0 session(s)" in format_gate(res)
