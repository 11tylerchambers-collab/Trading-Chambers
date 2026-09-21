"""Dashboard API against a seeded database. No network, no broker."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from chambers.clock import ET
from chambers.dashboard.app import create_app
from chambers.store import Bar, Store
from chambers.strategy import Params

NOW = datetime(2026, 9, 22, 10, 30, 5, tzinfo=ET)
D = date(2026, 9, 22)


def seed(path):
    st = Store(path)
    st.write_heartbeat(state="running", last_cycle_ts=NOW, cycles_today=60, signals_today=1200, fired_today=7,
                       opened_today=7, closed_today=5, open_positions=2, net_pnl_today=12.34, pid=4242,
                       started_at=NOW - timedelta(hours=1))
    st.write_params(Params().to_dict(), "config", NOW - timedelta(days=1))
    # today's bars for AAPL so the open position gets current price / vwap
    today = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
    st.write_bars("AAPL", [Bar(today + timedelta(minutes=i), 100, 100, 100, 100, 100) for i in range(30)]
                  + [Bar(today + timedelta(minutes=30), 99.5, 99.5, 99.5, 99.5, 100)])
    st.open_trade("AAPL", "long", 20, NOW - timedelta(minutes=3), 99.0, "o1", 98.99, 99.01,
                  {"dev_pct": -0.5, "vol_ratio": 2.0, "expect": "return to vwap", "expect_move_pct": 0.5,
                   "expect_within_bars": 10}, Params().to_dict())
    st.update_trade_progress(1, 3, -0.3, 0.6)
    st.open_trade("TSLA", "short", 8, NOW - timedelta(minutes=1), 250.0, "o2", None, None, {"x": 1}, {})
    for i in range(30):
        tid = st.open_trade("MSFT", "long", 5, NOW - timedelta(minutes=200 - i), 400.0, None, None, None,
                            {"expect": "return to vwap", "dev_pct": -0.4}, {})
        st.close_trade(tid, NOW - timedelta(minutes=190 - i), 401.0, None, 400.9, 401.1, "vwap_touch", 4, -0.1, 0.3,
                       5.0, 0.6, 4.4)
    st.write_params_history("2026-09-19", Params(entry_dev_pct=0.3).to_dict(), "sweep",
                            {"changed": False, "reason": "insufficient_evidence: 3 closed trades today", "results": [],
                             "today_closed_trades": 3, "days": ["2026-09-19"]})
    st.write_params_history("2026-09-21", Params(entry_dev_pct=0.4).to_dict(), "sweep",
                            {"changed": True, "reason": "moved_one_step_toward_best: entry_dev_pct 0.3->0.4",
                             "results": [{"params": {"entry_dev_pct": 0.4, "vol_mult": 1.5, "max_hold_bars": 10,
                                                     "stop_pct": 0.5}, "net_pnl": 55.5, "trades": 140,
                                          "trades_per_day": 70, "eligible": True}],
                             "today_closed_trades": 40, "days": ["2026-09-18", "2026-09-21"]})
    st.log_error("cycle.quotes", "BrokerError: quotes: timeout", "tb", NOW - timedelta(minutes=10))
    st.close()


@pytest.fixture
def client(tmp_path):
    p = tmp_path / "seed.db"
    seed(p)
    app = create_app(p, "hunter2")
    with TestClient(app) as c:
        yield c


def auth(client):
    r = client.post("/api/login", json={"password": "hunter2"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_index_serves_single_file(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    for section in ("Heartbeat", "Today", "Controls", "Open positions", "Recent trades", "Params", "Sweep history", "Errors"):
        assert section.lower() in body.lower()
    assert "<script src=" not in body and "<link" not in body  # no external resources, no build step


def test_login_required_and_wrong_password(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer bogus"}).status_code == 401
    assert client.post("/api/pause", json={"paused": True}).status_code == 401


def test_no_password_configured_refuses_login(tmp_path):
    p = tmp_path / "x.db"
    seed(p)
    with TestClient(create_app(p, "")) as c:
        assert c.post("/api/login", json={"password": ""}).status_code == 503


def test_state_has_every_section(client):
    h = auth(client)
    s = client.get("/api/state", headers=h).json()
    assert s["heartbeat"]["state"] == "running" and s["heartbeat"]["cycles_today"] == 60
    assert s["market"]["is_open"] is None  # no clock in this test
    assert s["account"] is None            # no broker in this test
    assert s["controls"] == {"paused": False, "flatten_requested": False, "updated_at": None}
    pos = {p["symbol"]: p for p in s["open_positions"]}
    assert set(pos) == {"AAPL", "TSLA"}
    a = pos["AAPL"]
    assert a["current_price"] == 99.5 and a["bars_held"] == 3 and a["bars_left"] == 7
    assert a["unrealized_pl"] == pytest.approx(0.5 * 20)
    assert a["to_vwap_pct"] == pytest.approx((a["vwap"] - 99.5) / 99.5 * 100)
    assert a["to_stop_pct"] == pytest.approx(0.5 + 0.5 / 99.0 * 100)  # in profit → further than stop_pct from the stop
    assert a["hypothesis"]["expect"] == "return to vwap"
    assert pos["TSLA"]["current_price"] is None and pos["TSLA"]["to_vwap_pct"] is None
    assert len(s["recent_trades"]) == 25
    t = s["recent_trades"][0]
    assert t["exit_reason"] == "vwap_touch" and t["hypothesis"]["expect"] == "return to vwap" and t["mae_pct"] == -0.1
    assert s["params"]["params"]["entry_dev_pct"] == 0.3 and s["params"]["source"] == "config"
    sw = s["sweep_history"]
    assert [x["date"] for x in sw] == ["2026-09-21", "2026-09-19"]
    assert sw[0]["delta"] == {"entry_dev_pct": [0.3, 0.4]} and sw[0]["replay_trades"] == 140 and sw[0]["replay_net_pnl"] == 55.5
    assert sw[1]["delta"] == {} and sw[1]["reason"].startswith("insufficient_evidence")
    assert len(s["errors"]) == 1 and s["errors"][0]["where_"] == "cycle.quotes"


def test_pause_and_flatten_write_controls(client):
    h = auth(client)
    assert client.post("/api/pause", json={"paused": True}, headers=h).json()["paused"] is True
    s = client.get("/api/state", headers=h).json()
    assert s["controls"]["paused"] is True and s["controls"]["updated_at"]
    client.post("/api/pause", json={"paused": False}, headers=h)
    assert client.get("/api/state", headers=h).json()["controls"]["paused"] is False
    assert client.post("/api/flatten", headers=h).json()["flatten_requested"] is True
    assert client.get("/api/state", headers=h).json()["controls"]["flatten_requested"] is True


def test_params_save_validates_and_writes_history(client, tmp_path):
    h = auth(client)
    r = client.post("/api/params", json={"params": {"entry_dev_pct": 0.45, "allow_short": False}}, headers=h)
    assert r.status_code == 200 and r.json()["params"]["entry_dev_pct"] == 0.45
    s = client.get("/api/state", headers=h).json()
    assert s["params"]["source"] == "manual" and s["params"]["params"]["allow_short"] is False
    assert s["params"]["params"]["vol_mult"] == 1.5  # untouched fields kept
    st = Store(tmp_path / "seed.db")
    hist = st.params_history(3)
    assert hist[0]["source"] == "manual" and hist[0]["params"]["entry_dev_pct"] == 0.45
    # validation
    assert client.post("/api/params", json={"params": {"entry_dev_pct": 0}}, headers=h).status_code == 400
    assert client.post("/api/params", json={"params": {"bogus": 1}}, headers=h).status_code == 400
    assert client.post("/api/params", json={"params": {"max_hold_bars": "abc"}}, headers=h).status_code == 400
    assert client.post("/api/params", json={"params": [1]}, headers=h).status_code == 400


def test_state_with_clock_and_broker(tmp_path):
    from .mocks import FakeTime, MockBroker, make_clock
    p = tmp_path / "seed.db"
    seed(p)
    broker = MockBroker()
    clock = make_clock(FakeTime(NOW), broker)
    with TestClient(create_app(p, "pw", broker, clock)) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"password": "pw"}).json()["token"]}
        s = c.get("/api/state", headers=h).json()
        assert s["market"]["is_open"] is True and s["market"]["entries_allowed"] is True
        assert s["market"]["flatten_at"].startswith("2026-09-22T15:55")
        assert s["account"] == {"portfolio_value": 100000.0, "buying_power": 200000.0}
        c.get("/api/state", headers=h)
        assert broker.calls["account"] == 1  # cached for 30s
