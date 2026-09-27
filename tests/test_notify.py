"""Telegram messages and alerts, the morning/evening text, and the heartbeat watchdog (PHASE1A §6)."""
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

import chambers.notify as notify_mod
from chambers.bars import TF_1H
from chambers.clock import ET
from chambers.messages import evening_report, morning_brief
from chambers.notify import Notifier, telegram_sender
from chambers.runtime import register_sleeves
from chambers.store import Bar, Store
from chambers.watchdog import expected_marks, run_watchdog, stale_sleeves

from .mocks import FakeTime, MockBroker, make_clock

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=ET)
D = date(2026, 9, 22)
CFG = {"universe": ["AAPL", "SPY"], "sleeves": {}}


def test_disabled_cleanly_without_token(tmp_path, caplog):
    st = Store(tmp_path / "t.db")
    with caplog.at_level(logging.WARNING, logger="chambers.notify"):
        n = Notifier(st)
    assert sum("not configured" in r.message for r in caplog.records) == 1
    assert n.enabled is False
    assert n.send("morning", "morning", "hello", NOW) == "disabled"
    assert n.alert("daily_loss", "x", NOW) == "disabled"
    assert [a["status"] for a in st.recent_alerts(5)] == ["disabled", "disabled"]


def test_alert_rate_limit_is_per_key_and_across_processes(tmp_path):
    st = Store(tmp_path / "t.db")
    sent = []
    a = Notifier(st, sender=sent.append)
    b = Notifier(Store(tmp_path / "t.db"), sender=sent.append)          # e.g. the watchdog process
    assert a.alert("recon", "mismatch 1", NOW) == "sent"
    assert b.alert("recon", "mismatch 2", NOW + timedelta(minutes=5)) == "suppressed"
    assert a.alert("stale:S0", "stale", NOW + timedelta(minutes=5)) == "sent"
    assert a.alert("recon", "mismatch 3", NOW + timedelta(minutes=16)) == "sent"
    assert sent == ["mismatch 1", "stale", "mismatch 3"]
    assert [x["status"] for x in st.recent_alerts(10)][::-1] == ["sent", "suppressed", "sent", "sent"]


def test_failed_send_is_recorded_not_raised(tmp_path):
    st = Store(tmp_path / "t.db")

    def boom(text):
        raise OSError("network down")

    assert Notifier(st, sender=boom).send("evening", "evening", "x", NOW) == "failed"
    assert "network down" in st.recent_alerts(1)[0]["error"]


def test_dry_run_prints_text(tmp_path):
    out = []
    n = Notifier(Store(tmp_path / "t.db"), dry_run=True, echo=out.append)
    assert n.send("evening", "evening", "  report text  ", NOW) == "dry_run" and out == ["report text"]


def test_long_message_split(tmp_path):
    sent = []
    Notifier(Store(tmp_path / "t.db"), sender=sent.append).send("evening", "evening", "x" * 9000, NOW)
    assert [len(s) for s in sent] == [4000, 4000, 1000]


def test_telegram_sender_request(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["url"], seen["data"], seen["timeout"] = req.full_url, req.data.decode(), timeout
        return Resp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    telegram_sender("123:ABC", "42")("hi there")
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert "chat_id=42" in seen["data"] and "text=hi+there" in seen["data"] and seen["timeout"] == 10.0

    def bad(req, timeout):
        return Resp(json.dumps({"ok": False, "description": "chat not found"}).encode())
    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", bad)
    with pytest.raises(RuntimeError, match="chat not found"):
        telegram_sender("1:A", "2")("x")


def seeded(tmp_path):
    st = Store(tmp_path / "t.db")
    register_sleeves(st, CFG)
    st.replace_econ_events([{"date": "2026-09-22", "event": "CPI (Aug)", "time": "08:30"}])
    prev = datetime(2026, 9, 21, 15, 59, tzinfo=ET)
    st.write_bars("SPY", [Bar(prev, 500, 500, 500, 500, 1)])
    st.write_bars_tf("BTC/USD", TF_1H, [Bar(datetime(2026, 9, 21, 15, 0, tzinfo=ET), 1e5, 1e5, 1e5, 1e5, 1)])
    st.open_trade("GLD", "long", 12, datetime(2026, 9, 21, 13, 30, 5, tzinfo=ET), 204.5, "o", None, None, {}, {},
                  sleeve_id="S3")
    st.write_params({"fast": 10, "slow": 30, "stop_atr": 2.5}, "config", NOW, "S3")
    return st


def test_morning_brief_text(tmp_path):
    st = seeded(tmp_path)
    st.write_alert(NOW - timedelta(hours=3), "alert", "recon", "RECON MISMATCH x", "sent")
    broker = MockBroker(prices={"SPY": 505.0, "BTC/USD": 101000.0, "QQQ": 400, "GLD": 205, "USO": 70})
    clk = make_clock(FakeTime(NOW.replace(hour=8, minute=45)), broker)
    text = morning_brief(st, broker, clk, NOW.replace(hour=8, minute=45))
    assert "SPY 505.00 (+1.00%)" in text and "BTC 101,000.00 (+1.00%)" in text
    assert "CPI (Aug) 08:30 - NEWS DAY" in text
    assert "S3 Trend following: GLD long 12@204.50" in text and "fast=10 slow=30 stop_atr=2.5" in text
    assert "Alerts since last evening: 1" in text and "RECON MISMATCH" in text
    assert max(len(l) for l in text.splitlines()) < 200 and "|" not in text


def test_evening_report_text(tmp_path):
    st = seeded(tmp_path)
    t = datetime(2026, 9, 22, 11, 0, tzinfo=ET)
    st.write_cycle(t, "running", 2, 1, 1, 0, 10, "S1")
    tid = st.open_trade("SPY", "long", 10, t, 500, "o", None, None, {}, {}, sleeve_id="S1")
    st.close_trade(tid, t + timedelta(minutes=30), 502, "x", None, None, "z_revert", 2, 0, 1, 20, 0.2, 19.8)
    tw = st.open_twin_trade("S1", "2026-09-22", 7, "QQQ", "short", 5, t, 400, {}, {}, True, "CPI (Aug)")
    st.close_twin_trade(tw, t + timedelta(minutes=30), 401, "z_revert", 2, -1, 0, -5, 0.1, -5.1)
    st.write_params_history(D, {"entry_z": 2.5}, "sweep", {"changed": True, "reason": "moved_to_best: entry_z 2.0->2.5"},
                            "S1")
    st.write_recon(t + timedelta(hours=6), "equity_session", "pass", equity=100000, diff=0.42, threshold=10.0)
    st.set_risk_halt(D, t, {"pnl": -2100})
    st.write_signals(1, [{"ts": t, "symbol": "AAPL", "reason": "same_side_cap"}] * 3, "S0")
    st.write_lab_suggestion(1, 1, "L1_orb", t, {"range_end": "10:30"}, 24, 150.0, -20.0, 31)
    st.write_p100_day(D, {"start_equity": 100, "end_equity": 101.5, "settled_cash_start": 100, "settled_cash_end": 0,
                          "unsettled": [[101.5, "2026-09-23"]], "trades": 2, "skipped": 3, "net_pnl": 1.5,
                          "shadow_trades": 1, "shadow_net": -0.4}, [])
    st.log_error("unhandled.S2", "boom", None, t)
    text = evening_report(st, D, 100123.45)
    assert "S1 Index mean reversion: 1 trades, net +19.80, twin -5.10, edge +24.90" in text
    assert "params: CHANGED moved_to_best: entry_z 2.0->2.5" in text
    assert "Recon: pass (diff +0.42" in text
    assert "DAILY LOSS HALT at 11:00" in text and "same_side_cap x3" in text
    assert "L1_orb: graded net +150.00 vs twin -20.00 over 24 sessions" in text
    assert "$100 profile: equity 101.50" in text and "3 skipped for cash" in text and "learning only" in text
    assert "Errors today: 1 (1 UNHANDLED)" in text and "equity 100,123.45" in text


def test_watchdog_stale_and_fresh(tmp_path):
    st = Store(tmp_path / "t.db")
    broker = MockBroker()
    clk = make_clock(FakeTime(NOW), broker)
    session = clk.today_session()
    assert len(expected_marks("S0", session, session.open, session.close)) == 385
    assert len(expected_marks("S1", session, session.open, session.close)) == 25
    assert len(expected_marks("S2", None, NOW, NOW + timedelta(days=1))) == 24
    st.write_heartbeat("S0", state="running", last_cycle_ts=NOW - timedelta(minutes=1, seconds=55))  # 09:58:05
    st.write_heartbeat("S1", state="running", last_cycle_ts=datetime(2026, 9, 22, 9, 45, 5, tzinfo=ET))
    st.write_heartbeat("S2", state="running", last_cycle_ts=datetime(2026, 9, 22, 9, 0, 11, tzinfo=ET))
    assert stale_sleeves(st, clk, ["S0", "S1", "S2", "S3"], NOW) == []
    sent = []
    n = Notifier(st, sender=sent.append)
    later = NOW + timedelta(minutes=5)                                     # S0 has not cycled since 09:58:05
    st.write_heartbeat("S1", last_cycle_ts=datetime(2026, 9, 22, 10, 0, 5, tzinfo=ET))
    st.write_heartbeat("S2", last_cycle_ts=datetime(2026, 9, 22, 10, 0, 12, tzinfo=ET))
    stale = run_watchdog(st, clk, n, ["S0", "S1", "S2", "S3"], later)
    assert [s["sleeve_id"] for s in stale] == ["S0"] and "S0 missed its 10:01:05 ET cycle" in sent[0]
    assert run_watchdog(st, clk, n, ["S0"], later + timedelta(minutes=1)) and len(sent) == 1   # rate-limited
    # S2 at night: 24/7
    night = datetime(2026, 9, 22, 23, 10, tzinfo=ET)
    assert [s["sleeve_id"] for s in stale_sleeves(st, clk, ["S0", "S2"], night)] == ["S2"]
