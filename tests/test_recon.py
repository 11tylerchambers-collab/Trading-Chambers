"""Broker reconciliation, nightly backups and the nightly job chain (PHASE1A §5.2, §5.3). No network."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from chambers.clock import ET
from chambers.jobs import MorningBrief, NightlyJobs, Step
from chambers.recon import backup_exists, nightly_backup, run_recon
from chambers.store import Store

from .mocks import FakeTime, MockBroker, make_clock

D = date(2026, 9, 22)
T0 = datetime(2026, 9, 21, 17, 0, tzinfo=ET)
T1 = datetime(2026, 9, 22, 17, 0, tzinfo=ET)


class Alerts:
    def __init__(self):
        self.sent = []

    def alert(self, key, msg, now=None):
        self.sent.append((key, msg))


def trade(st, sym, side, qty, entry, exit_, when, sleeve="S0"):
    tid = st.open_trade(sym, side, qty, when - timedelta(minutes=5), entry, "o", None, None, {"x": 1}, {}, sleeve_id=sleeve)
    g = (exit_ - entry) * qty * (1 if side == "long" else -1)
    st.close_trade(tid, when, exit_, "x", None, None, "vwap_touch", 1, 0, 0, g, 0.1, g - 0.1)
    return g


class PosBroker(MockBroker):
    def __init__(self):
        super().__init__()
        self.unreal = 0.0

    def positions(self):
        self._hit("positions")
        return [{"symbol": "GLD", "qty": 10, "avg_entry_price": 200, "current_price": 201,
                 "unrealized_pl": self.unreal}] if self.unreal else []


def test_recon_baseline_pass_and_mismatch_alert(tmp_path):
    st = Store(tmp_path / "t.db")
    b = PosBroker()
    al = Alerts()
    assert run_recon(st, b, T0, "equity_session", notifier=al)["status"] == "baseline"
    # the day: S0 made +120 gross, S1 lost −20, an S3 position is up +35 unrealized
    g = trade(st, "AAPL", "long", 20, 100, 106, T0 + timedelta(hours=17)) + \
        trade(st, "SPY", "short", 10, 500, 502, T0 + timedelta(hours=18), "S1")
    b.unreal = 35.0
    b.equity = 100000 + g + 35.0 + 3.0          # $3 of noise: within max($5, 0.01% of equity)
    r = run_recon(st, b, T1, "equity_session", notifier=al)
    assert r["status"] == "pass" and r["realized"] == pytest.approx(100.0) and r["diff"] == pytest.approx(3.0)
    row = st.last_recon()
    assert row["status"] == "pass" and row["detail"]["realized_gross_by_sleeve"] == {"S0": 120.0, "S1": -20.0}
    assert al.sent == []
    # next day equity is $40 short of what the books say → mismatch + alert with the numbers
    b.equity -= 40.0
    r = run_recon(st, b, T1 + timedelta(days=1), "equity_session", notifier=al)
    assert r["status"] == "mismatch" and r["diff"] == pytest.approx(-40.0)
    assert al.sent[0][0] == "recon:equity_session" and "-40.00" in al.sent[0][1]


def test_recon_counts_crypto_fees_and_chains_kinds(tmp_path):
    st = Store(tmp_path / "t.db")
    b = PosBroker()
    run_recon(st, b, T0, "equity_session")
    g = trade(st, "BTC/USD", "long", 0.1, 100000, 101000, T0 + timedelta(hours=5), "S2")      # +100 gross
    fees = 0.0025 * 0.1 * (100000 + 101000)                                                      # 50.25
    b.equity = 100000 + g - fees
    r = run_recon(st, b, T0 + timedelta(hours=8), "crypto_rollover", fee_rate=0.0025)
    assert r["status"] == "pass" and r["fees"] == pytest.approx(fees)
    assert st.last_recon()["prev_ts"] == st.recent_recon(2)[1]["ts"]   # measured from the equity-session recon


def test_recon_broker_error_is_recorded(tmp_path):
    st = Store(tmp_path / "t.db")
    b = MockBroker(fail=["account"])
    assert run_recon(st, b, T0, "equity_session")["status"] == "error"
    assert st.recent_recon(1)[0]["status"] == "error" and st.last_recon() is None


def test_nightly_backup_uses_backup_api_and_keeps_14(tmp_path):
    st = Store(tmp_path / "chambers.db")
    st.log_error("x", "hello", None, T0)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "pre-phase1a-20260920-120000.db").write_bytes(b"keep me")
    for i in range(16):
        nightly_backup(st, bdir, date(2026, 9, 1) + timedelta(days=i))
    names = sorted(p.name for p in bdir.iterdir())
    assert len([n for n in names if n.startswith("chambers-")]) == 14
    assert names[0] == "chambers-2026-09-03.db" and "pre-phase1a-20260920-120000.db" in names
    con = sqlite3.connect(str(bdir / "chambers-2026-09-16.db"))
    assert con.execute("SELECT message FROM errors").fetchone()[0] == "hello"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    con.close()
    assert backup_exists(bdir, date(2026, 9, 16)) and not backup_exists(bdir, date(2026, 9, 2))


def test_nightly_chain_waits_for_sweeps_runs_in_order_and_once(tmp_path):
    st = Store(tmp_path / "t.db")
    ft = FakeTime(datetime(2026, 9, 22, 16, 30, tzinfo=ET))
    broker = MockBroker()
    clk = make_clock(ft, broker)
    ran = []
    files = set()
    steps = [Step("a", lambda d, now: ran.append("a")),
             Step("b", lambda d, now: (_ for _ in ()).throw(RuntimeError("boom"))),
             Step("c", lambda d, now: (ran.append("c"), files.add(d)), done=lambda d: d in files)]
    job = NightlyJobs(st, clk, steps, ["S0", "S1"])
    when, action = job.plan(ft.now)
    assert action is None and when == ft.now + timedelta(minutes=1)         # S0/S1 have not swept yet
    st.write_params_history(D, {}, "sweep", {}, "S0")
    st.write_params_history(D, {}, "sweep", {}, "S1")
    for _ in range(5):
        when, action = job.plan(ft.now)
        if action:
            action()
    assert ran == ["a", "c"] and st.errors_count(where="nightly.b") == 1
    assert job.plan(ft.now)[1] is None
    # a restarted process: 'c' is durably done; 'a' and 'b' have no durable check and run again
    job2 = NightlyJobs(st, clk, steps, ["S0", "S1"])
    names = []
    for _ in range(5):
        _, action = job2.plan(ft.now)
        if action:
            names.append(action)
            action()
    assert ran == ["a", "c", "a"]


def test_morning_brief_once_per_session_day(tmp_path):
    st = Store(tmp_path / "t.db")
    ft = FakeTime(datetime(2026, 9, 22, 7, 0, tzinfo=ET))
    broker = MockBroker()
    clk = make_clock(ft, broker)
    sent = []

    def send(now):
        sent.append(now)
        st.write_alert(now, "morning", "morning", "brief", "sent")

    mb = MorningBrief(st, clk, send)
    when, action = mb.plan(ft.now)
    assert when == datetime(2026, 9, 22, 8, 45, tzinfo=ET) and action is None
    ft.now = when
    when, action = mb.plan(ft.now)
    action()
    when, action = mb.plan(ft.now)
    assert len(sent) == 1 and action is None
    assert when > ft.now                       # never today's 8:45 again (that starved S2's 9:00 cycle)


class HourlyStub:
    """Stands in for S2: wants the loop at 9:00:10 on a session day."""
    sleeve_id = "S2"
    state = "running"

    def __init__(self, ft):
        self.ft, self.ran = ft, []

    def plan(self, now):
        mark = datetime(2026, 9, 22, 9, 0, 10, tzinfo=ET)
        if not self.ran and now < mark:
            return mark, (lambda: self.ran.append(self.ft.now))
        if not self.ran:
            return now, (lambda: self.ran.append(self.ft.now))
        return now + timedelta(hours=1), None

    def stop(self):
        pass


def test_morning_brief_does_not_starve_the_9am_cycle(tmp_path):
    from chambers.scheduler import Scheduler
    st = Store(tmp_path / "t.db")
    ft = FakeTime(datetime(2026, 9, 22, 8, 40, tzinfo=ET))
    broker = MockBroker()
    clk = make_clock(ft, broker)
    mb = MorningBrief(st, clk, lambda now: st.write_alert(now, "morning", "morning", "brief", "sent"))
    s2 = HourlyStub(ft)
    sch = Scheduler(st, broker, clk, [s2], jobs=[mb], sleep_fn=ft.sleep)
    ticks = 0
    while not s2.ran and ticks < 1000:
        sch.tick()
        ticks += 1
    assert s2.ran == [datetime(2026, 9, 22, 9, 0, 10, tzinfo=ET)] and ticks < 10
