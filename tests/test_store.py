from datetime import datetime, timedelta

from chambers.store import ET, Bar, Store, iso, parse_ts

NOW = datetime(2026, 9, 22, 10, 0, 5, tzinfo=ET)


def test_iso_roundtrip_and_utc_conversion():
    from zoneinfo import ZoneInfo
    utc = datetime(2026, 9, 22, 14, 0, tzinfo=ZoneInfo("UTC"))
    s = iso(utc)
    assert s == "2026-09-22T10:00:00-04:00"
    assert parse_ts(s) == utc


def test_cycles(tmp_path):
    st = Store(tmp_path / "t.db")
    cid = st.write_cycle(NOW, "running", 20, 2, 1, 0, 123)
    assert cid == 1
    rows = st.cycles_for_day("2026-09-22")
    assert len(rows) == 1 and rows[0]["symbols_evaluated"] == 20 and rows[0]["duration_ms"] == 123
    assert st.cycles_for_day("2026-09-23") == []
    assert st.cycle_dates() == ["2026-09-22"]


def test_signals(tmp_path):
    st = Store(tmp_path / "t.db")
    st.write_signals(1, [
        {"ts": NOW, "symbol": "AAPL", "close": 100.0, "vwap": 101.0, "dev_pct": -0.99, "vol_ratio": 2.0,
         "side": "long", "fired": True, "reason": "fired", "params": {"a": 1}},
        {"ts": NOW, "symbol": "MSFT", "close": None, "vwap": None, "dev_pct": None, "vol_ratio": None,
         "side": None, "fired": False, "reason": "insufficient_bars", "params": {"a": 1}},
    ])
    rows = st.signals_for_day("2026-09-22")
    assert len(rows) == 2
    assert rows[0]["fired"] == 1 and rows[0]["params_json"] == '{"a": 1}'
    assert rows[1]["reason"] == "insufficient_bars" and rows[1]["close"] is None
    assert st.signals_count_for_day("2026-09-22") == 2
    assert st.signals_count_for_day("2026-09-22", fired_only=True) == 1


def test_trades_open_progress_close(tmp_path):
    st = Store(tmp_path / "t.db")
    tid = st.open_trade("AAPL", "long", 20, NOW, 100.0, "o1", 99.99, 100.01,
                        {"expect": "return to vwap"}, {"stop_pct": 0.5})
    ot = st.open_trades()
    assert len(ot) == 1 and ot[0].id == tid and ot[0].is_open and ot[0].to_dict()["hypothesis"]["expect"] == "return to vwap"
    st.update_trade_progress(tid, 3, -0.2, 0.1)
    t = st.get_trade(tid)
    assert (t.bars_held, t.mae_pct, t.mfe_pct) == (3, -0.2, 0.1)
    st.close_trade(tid, NOW + timedelta(minutes=5), 101.0, "o2", 100.99, 101.01, "vwap_touch",
                   5, -0.2, 1.0, 20.0, 0.6, 19.4)
    assert st.open_trades() == []
    closed = st.closed_trades_for_day("2026-09-22")
    assert len(closed) == 1 and closed[0].exit_reason == "vwap_touch" and closed[0].net_pnl == 19.4
    assert st.recent_closed_trades(25)[0].id == tid
    assert st.opened_count_for_day("2026-09-22") == 1


def test_bars_idempotent_and_dates(tmp_path):
    st = Store(tmp_path / "t.db")
    bars = [Bar(NOW + timedelta(minutes=i), 1, 2, 0.5, 1.5, 100) for i in range(3)]
    assert st.write_bars("AAPL", bars) == 3
    assert st.write_bars("AAPL", bars) == 0  # primary key ignores duplicates
    st.write_bars("AAPL", [Bar(NOW - timedelta(days=1), 1, 2, 0.5, 1.5, 100)])
    by = st.bars_for_day("2026-09-22")
    assert list(by) == ["AAPL"] and len(by["AAPL"]) == 3
    assert by["AAPL"][0].ts == NOW and by["AAPL"][0].c == 1.5
    assert st.bar_dates() == ["2026-09-21", "2026-09-22"]
    assert st.latest_bar_ts("2026-09-22") == iso(NOW + timedelta(minutes=2))
    assert st.bars_for_day("2026-09-22", "MSFT") == {}


def test_heartbeat(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st.read_heartbeat() is None
    st.write_heartbeat(state="preopen", pid=42, started_at=NOW)
    st.write_heartbeat(state="running", last_cycle_ts=NOW, cycles_today=1)
    hb = st.read_heartbeat()
    assert hb["state"] == "running" and hb["pid"] == 42 and hb["cycles_today"] == 1
    assert hb["last_cycle_ts"] == iso(NOW) and hb["started_at"] == iso(NOW)
    try:
        st.write_heartbeat(bogus=1)
        assert False
    except ValueError:
        pass


def test_params_and_history(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st.read_params() is None
    st.write_params({"entry_dev_pct": 0.3}, "config", NOW)
    st.write_params({"entry_dev_pct": 0.4}, "sweep", NOW + timedelta(hours=1))
    p = st.read_params()
    assert p["params"] == {"entry_dev_pct": 0.4} and p["source"] == "sweep"
    st.write_params_history("2026-09-22", {"entry_dev_pct": 0.4}, "sweep", {"reason": "best"})
    st.write_params_history("2026-09-22", {"entry_dev_pct": 0.5}, "manual", None)
    h = st.params_history(7)
    assert len(h) == 2 and h[0]["source"] == "manual" and h[1]["sweep_summary"] == {"reason": "best"}
    assert len(st.params_history(7, source="sweep")) == 1
    assert st.params_history_dates("sweep") == ["2026-09-22"]


def test_controls(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st.read_controls() == {"paused": False, "flatten_requested": False, "updated_at": None}
    st.set_paused(True, NOW)
    st.request_flatten(NOW)
    c = st.read_controls()
    assert c["paused"] is True and c["flatten_requested"] is True and c["updated_at"] == iso(NOW)
    st.clear_flatten_request(NOW)
    st.set_paused(False, NOW)
    c = st.read_controls()
    assert c["paused"] is False and c["flatten_requested"] is False


def test_errors(tmp_path):
    st = Store(tmp_path / "t.db")
    st.log_error("cycle", "boom", "tb", NOW)
    st.log_error("reconcile", "orphan", None, NOW)
    errs = st.recent_errors(10)
    assert len(errs) == 2 and errs[0]["where_"] == "reconcile"
    assert st.errors_count(where="cycle") == 1
    assert st.errors_count(where="unhandled", d="2026-09-22") == 0
    assert st.errors_count(d="2026-09-22") == 2


def test_wal_mode_on_file_db(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st._one("PRAGMA journal_mode")[0] == "wal"
