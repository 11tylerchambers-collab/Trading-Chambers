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


def test_sweep_delta_is_against_the_params_the_sweep_started_from(tmp_path):
    # 2026-09-23/24: a manual restore sits between two sweep rows; the diff must use the sweep's `current`
    p = tmp_path / "s.db"
    st = Store(p)
    st.write_params_history("2026-09-23", Params(entry_dev_pct=0.5, vol_mult=1.0).to_dict(), "sweep",
                            {"changed": True, "current": Params(entry_dev_pct=0.4, vol_mult=1.25).to_dict()})
    st.write_params_history("2026-09-23", Params(entry_dev_pct=0.4, vol_mult=1.25).to_dict(), "manual", None)
    st.write_params_history("2026-09-24", Params(entry_dev_pct=0.5, vol_mult=1.5).to_dict(), "sweep",
                            {"changed": True, "current": Params(entry_dev_pct=0.4, vol_mult=1.25).to_dict()})
    st.close()
    with TestClient(create_app(p, "pw")) as c:
        h = {"Authorization": "Bearer " + c.post("/api/login", json={"password": "pw"}).json()["token"]}
        sw = c.get("/api/state", headers=h).json()["sweep_history"]
    assert sw[0]["delta"] == {"entry_dev_pct": [0.4, 0.5], "vol_mult": [1.25, 1.5]}
    assert sw[1]["delta"] == {"entry_dev_pct": [0.4, 0.5], "vol_mult": [1.25, 1.0]}


# ---------------------------------------------------------------- Phase 1A additions

def seed_1a(path):
    from chambers.runtime import register_sleeves
    seed(path)
    st = Store(path)
    register_sleeves(st, {"universe": ["AAPL", "MSFT"], "sleeves": {}})
    st.write_heartbeat("S1", state="running", last_cycle_ts=NOW - timedelta(minutes=5), cycles_today=4)
    st.write_heartbeat("ALL", state="running", last_cycle_ts=NOW, pid=4242)
    st.write_bars("SPY", [Bar(NOW.replace(minute=15, second=0), 500, 500, 500, 500, 1)])
    st.open_trade("SPY", "long", 10, NOW - timedelta(minutes=15), 498.0, "s1", None, None,
                  {"stop_price": 495.0, "z": -2.3, "entry_bar_ts": "2026-09-22T10:00:00-04:00"},
                  {"entry_z": 2.0}, sleeve_id="S1")
    t = st.open_trade("QQQ", "short", 5, NOW - timedelta(minutes=60), 400.0, "s1b", None, None, {"z": 2.5}, {},
                      sleeve_id="S1")
    st.close_trade(t, NOW - timedelta(minutes=30), 398.0, "x", None, None, "z_revert", 2, 0, 0.5, 10.0, 0.1, 9.9)
    w = st.open_twin_trade("S1", "2026-09-22", 9, "SPY", "short", 3, NOW - timedelta(minutes=60), 500.0, {}, {}, False, None)
    st.close_twin_trade(w, NOW - timedelta(minutes=30), 501.0, "z_revert", 2, -0.2, 0, -3.0, 0.1, -3.1)
    st.write_recon(NOW, "equity_session", "pass", equity=100000, diff=0.5, threshold=10)
    st.write_alert(NOW, "alert", "recon", "RECON MISMATCH test", "disabled")
    st.start_lab_run(NOW, ["2026-09-21"])
    st.write_lab_result(1, "L1_orb", 24, [], [], {"range_minutes": 60}, 5, 50.0, 20, -4.0, 1, True, "ok", None)
    st.write_lab_suggestion(1, 1, "L1_orb", NOW, {"range_minutes": 60}, 24, 50.0, -4.0, 20)
    st.write_p100_day(D, {"start_equity": 100, "end_equity": 100.4, "settled_cash_start": 100, "settled_cash_end": 0,
                          "unsettled": [[100.4, "2026-09-23"]], "trades": 1, "skipped": 2, "net_pnl": 0.4,
                          "shadow_trades": 1, "shadow_net": -0.2}, [])
    st.close()


@pytest.fixture
def client_1a(tmp_path):
    p = tmp_path / "seed.db"
    seed_1a(p)
    with TestClient(create_app(p, "pw", now_fn=lambda: NOW)) as c:
        c.h = {"Authorization": "Bearer " + c.post("/api/login", json={"password": "pw"}).json()["token"]}
        c.path = p
        yield c


def test_views_all_sleeve_and_p100(client_1a):
    c = client_1a
    s = c.get("/api/state?sleeve=S1", headers=c.h).json()
    assert s["sleeve"] == "S1" and s["heartbeat"]["cycles_today"] == 4
    assert [p["symbol"] for p in s["open_positions"]] == ["SPY"]
    pos = s["open_positions"][0]
    assert pos["stop_price"] == 495.0 and pos["current_price"] == 500 and pos["unrealized_pl"] == pytest.approx(20.0)
    assert [t["symbol"] for t in s["recent_trades"]] == ["QQQ"]
    assert s["edge"]["sleeve_net"] == 9.9 and s["edge"]["twin_net"] == -3.1 and s["edge"]["edge"] == 13.0
    assert s["params"]["sleeve_id"] == "S1" and s["params"]["params"]["entry_z"] == 2.0
    assert {f["key"] for f in s["params"]["fields"]} >= {"entry_z", "stop_atr", "max_hold_bars", "skip_news_days"}
    ov = {x["id"]: x for x in s["sleeves"]}
    assert set(ov) == {"S0", "S1", "S2", "S3"} and ov["S1"]["edge_today"] == 13.0 and ov["S1"]["open_positions"] == 1
    assert s["portfolio"]["long"] == 2 and s["portfolio"]["short"] == 1 and s["portfolio"]["last_recon"]["status"] == "pass"
    assert s["lab"]["suggestions"][0]["candidate"] == "L1_orb" and s["alerts"][0]["message"] == "RECON MISMATCH test"
    a = c.get("/api/state?sleeve=All", headers=c.h).json()
    assert {p["sleeve_id"] for p in a["open_positions"]} == {"S0", "S1"} and a["params"] is None
    assert a["heartbeat"]["pid"] == 4242
    p = c.get("/api/state?sleeve=P100", headers=c.h).json()
    assert p["p100"]["ledger"][0]["end_equity"] == 100.4 and set(p["p100"]["params"]) == {"S0", "S1"}
    assert c.get("/api/state?sleeve=S9", headers=c.h).status_code == 400
    # no `sleeve` = S0, the Phase 0 contract
    assert c.get("/api/state", headers=c.h).json()["sleeve"] == "S0"


def test_edit_bar_sleeve_params(client_1a):
    c = client_1a
    r = c.post("/api/params", json={"sleeve_id": "S3", "params": {"fast": 5, "slow": 20}}, headers=c.h)
    assert r.status_code == 200 and r.json()["params"]["fast"] == 5
    st = Store(c.path)
    assert st.read_params("S3")["source"] == "manual" and st.params_history(1, "manual", "S3")[0]["params"]["slow"] == 20
    assert st.read_params("S0")["source"] == "config"                     # other sleeves untouched
    assert c.post("/api/params", json={"sleeve_id": "S3", "params": {"fast": 30, "slow": 20}}, headers=c.h).status_code == 400
    assert c.post("/api/params", json={"sleeve_id": "S1", "params": {"entry_dev_pct": 1}}, headers=c.h).status_code == 400
    assert c.post("/api/params", json={"sleeve_id": "P100", "params": {}}, headers=c.h).status_code == 400


def test_lab_approve_and_reject(client_1a):
    c = client_1a
    st = Store(c.path)
    big = st.write_lab_suggestion(1, 2, "L2_pullback", NOW, {"down_days": 3}, 60, 300.0, -80.0, 30)
    assert c.post("/api/lab/decide", json={"id": big, "status": "approved"}).status_code == 401
    assert c.post("/api/lab/decide", json={"id": big, "status": "maybe"}, headers=c.h).status_code == 400
    assert c.post("/api/lab/decide", json={"id": big, "status": "approved"}, headers=c.h).json()["status"] == "approved"
    assert c.post("/api/lab/decide", json={"id": big, "status": "rejected"}, headers=c.h).status_code == 409
    sug = st.lab_suggestion(big)
    assert sug["status"] == "approved" and sug["decided_at"]
    assert c.get("/api/state", headers=c.h).json()["lab"]["min_approve_trades"] == 30


def test_lab_approval_needs_30_graded_trades(client_1a):
    c = client_1a                               # suggestion 1 was graded on 20 trades
    r = c.post("/api/lab/decide", json={"id": 1, "status": "approved"}, headers=c.h)
    assert r.status_code == 409 and "30 graded trades" in r.json()["detail"] and "has 20" in r.json()["detail"]
    st = Store(c.path)
    assert st.lab_suggestion(1)["status"] == "pending"
    assert st.decide_lab_suggestion(1, "approved", NOW) is False          # the store enforces it too
    assert c.post("/api/lab/decide", json={"id": 1, "status": "rejected"}, headers=c.h).json()["status"] == "rejected"
    # approving records the decision only: no params, trades or sleeves change
    assert Store(c.path).read_params("S1") is None
