"""Multi-cadence scheduler: S0 keeps its Phase 0 day inside the scheduler, other cadences interleave,
and an exception in one runner never stops another. No network."""
from datetime import date, datetime, timedelta, timezone

from chambers.clock import ET
from chambers.engine import Engine
from chambers.scheduler import Scheduler
from chambers.store import Store
from chambers.strategy import Params

from .mocks import FakeTime, MockBroker, flat_bars, make_clock

D = date(2026, 9, 22)
OPEN = datetime(2026, 9, 22, 9, 30, tzinfo=ET)
CFG = Params().to_dict()


class Hourly:
    """A 24/7 runner that cycles at hh:00:10 UTC (the S2 cadence)."""
    sleeve_id = "SX"

    def __init__(self, ft, fail_plan=False, fail_action=False):
        self.ft, self.fail_plan, self.fail_action = ft, fail_plan, fail_action
        self.runs, self.state, self.positions, self.last = [], "running", {}, None

    def plan(self, now):
        if self.fail_plan:
            raise RuntimeError("plan bug")
        u = now.astimezone(timezone.utc)
        mark = u.replace(minute=0, second=10, microsecond=0)
        if mark <= u and mark != self.last and (u - mark).total_seconds() < 600:
            return now, lambda: self._run(mark)
        if mark <= u:
            mark += timedelta(hours=1)
        return mark.astimezone(ET), lambda: self._run(mark)

    def _run(self, mark):
        if self.fail_action:
            raise RuntimeError("action bug")
        self.last = mark
        self.runs.append(self.ft.now)

    def startup(self, reconcile=True):
        pass

    def stop(self):
        pass

    def flatten(self, reason, now, safety_net=False):
        self.flattened = reason
        return 0


def make(tmp_path, start, extra=()):
    broker = MockBroker(bars={"AAPL": flat_bars(100.0, OPEN, 400)})
    ft = FakeTime(start)
    st = Store(tmp_path / "t.db")
    sweeps = []

    def fake_sweep(store, params, d, now):
        sweeps.append(d)
        store.write_params_history(d, params.to_dict(), "sweep", {"reason": "test"})
        return {"reason": "test"}

    clk = make_clock(ft, broker)
    s0 = Engine(st, broker, clk, ["AAPL"], CFG, sleep_fn=ft.sleep, sweep_fn=fake_sweep)
    runners = [s0] + [x(ft) for x in extra]
    sch = Scheduler(st, broker, clk, runners, sleep_fn=ft.sleep)
    return sch, s0, runners, st, broker, ft, sweeps


def test_s0_day_inside_scheduler_with_hourly_runner(tmp_path):
    sch, s0, runners, st, broker, ft, sweeps = make(tmp_path, datetime(2026, 9, 22, 9, 0, tzinfo=ET), [Hourly])
    hourly = runners[1]
    sch.startup()
    assert st.read_heartbeat("ALL")["pid"] is not None
    while ft.now < datetime(2026, 9, 22, 16, 45, tzinfo=ET):
        sch.tick()
    cycles = st.cycles_for_day(D)
    # every minute 9:30..15:54 exactly once, same as Phase 0
    assert len(cycles) == 385
    minutes = [c["ts"][11:19] for c in cycles]
    assert minutes[0] == "09:30:05" and minutes[-1] == "15:54:05" and len(set(minutes)) == 385
    assert s0._flattened == D and sweeps == [D] and broker.close_all_calls == 1
    # hourly runner ran at every hh:00:10 UTC in the window (9:00 ET .. 16:45 ET = 13:00Z .. 20:45Z)
    assert [t.astimezone(timezone.utc).strftime("%H:%M:%S") for t in hourly.runs] == \
           [f"{h}:00:10" for h in range(13, 21)]
    hb = st.read_heartbeats()
    assert hb["S0"]["cycles_today"] == 385 and hb["ALL"]["last_cycle_ts"] == hb["S0"]["last_cycle_ts"]
    assert st.errors_count(where="unhandled") == 0


def test_a_crashing_runner_never_stops_s0(tmp_path):
    def broken_plan(ft):
        r = Hourly(ft, fail_plan=True)
        r.sleeve_id = "SBAD"
        return r

    def broken_action(ft):
        r = Hourly(ft, fail_action=True)
        r.sleeve_id = "SBAD2"
        return r

    sch, s0, runners, st, broker, ft, sweeps = make(tmp_path, datetime(2026, 9, 22, 9, 55, tzinfo=ET),
                                                    [broken_plan, broken_action])
    sch.startup()
    while ft.now < datetime(2026, 9, 22, 11, 0, 30, tzinfo=ET):
        sch.tick()
    cycles = st.cycles_for_day(D)
    assert len(cycles) == 66                     # 09:55:05 .. 11:00:05, none lost
    assert st.errors_count(where="unhandled.SBAD") > 0
    assert st.errors_count(where="unhandled.SBAD2") >= 1
    assert st.errors_count(where="unhandled") == 0


def test_late_minute_is_caught_up_after_a_slow_runner(tmp_path):
    sch, s0, runners, st, broker, ft, sweeps = make(tmp_path, datetime(2026, 9, 22, 10, 0, 0, tzinfo=ET))
    sch.startup()
    sch.tick()                                    # 10:00:05 cycle
    ft.advance(seconds=70)                         # something ran for 70 s: it is now 10:01:15
    sch.tick()                                    # the 10:01 cycle runs late instead of being skipped
    ts = [c["ts"][11:19] for c in st.cycles_for_day(D)]
    assert ts == ["10:00:05", "10:01:15"]
    sch.tick()
    assert [c["ts"][11:19] for c in st.cycles_for_day(D)][-1] == "10:02:05"


def test_dashboard_flatten_request_flattens_every_sleeve(tmp_path):
    sch, s0, runners, st, broker, ft, sweeps = make(tmp_path, datetime(2026, 9, 22, 10, 0, 0, tzinfo=ET), [Hourly])
    sch.startup()
    st.request_flatten(ft.now)
    sch.tick()
    assert runners[1].flattened == "manual_flatten"
    assert broker.close_all_calls == 1            # S0 last, with the safety net
    assert st.read_controls()["flatten_requested"] is False
    assert s0.handle_flatten_requests is False
