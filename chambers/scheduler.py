"""Multi-cadence scheduler (PHASE1A §10): one process, several sleeves, independent cycles.

Every runner (a sleeve, or a job such as the nightly chain or the morning brief) implements
`plan(now) -> (when, action)`: it does whatever is due right now that is not a cycle (preopen,
flatten, sweep) and says when it next needs the loop and what to run then. The scheduler asks
every runner, waits until the earliest `when`, runs that one action, and asks again. Runners are
asked in list order, so S0 goes first when two are due at the same moment.

Isolation: every `plan` and every action is wrapped; an exception is logged as
`unhandled.<runner>` (it escaped the runner's own handling, which §15.7 counts) and the other
runners carry on. Only an exception in the scheduler's own code is `unhandled`.

The scheduler is also the coordinator the sleeves share: reconcile runs across all sleeves at
once (the broker holds their sum), and the dashboard's "Flatten now" flattens every sleeve.
"""
from __future__ import annotations

import logging
import os
import time
import traceback
from datetime import datetime, timedelta
from typing import Callable, Optional

from .reconcile import reconcile_sleeves

log = logging.getLogger("chambers.scheduler")

WAIT_CHUNK_S = 30.0
PLAN_RETRY_S = 60.0


class Scheduler:
    def __init__(self, store, broker, clock, runners: list, jobs: Optional[list] = None,
                 sleep_fn: Callable[[float], None] = time.sleep, notifier=None):
        self.store = store
        self.broker = broker
        self.clock = clock
        self.runners = list(runners)          # sleeves; runners[0] is S0
        self.jobs = list(jobs or [])          # non-sleeve runners (nightly chain, messages)
        self._sleep = sleep_fn
        self.notifier = notifier
        self._stop = False
        self.started_at: Optional[datetime] = None
        self._backoff: dict[int, datetime] = {}   # runner index -> not before (after an exception)
        for r in self.runners:
            r.coordinator = self
            r.handle_flatten_requests = False

    # ---- helpers -------------------------------------------------------------
    def now(self) -> datetime:
        return self.clock.now_et()

    @staticmethod
    def name(r) -> str:
        return getattr(r, "sleeve_id", None) or getattr(r, "name", type(r).__name__)

    def _log_error(self, where: str, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        log.error("%s: %s", where, msg)
        try:
            now = self.now()
            self.store.log_error(where, msg, traceback.format_exc(), now)
            self.store.write_heartbeat("ALL", last_error=f"{where}: {msg}"[:500], last_error_ts=now)
            if where.startswith("unhandled") and self.notifier is not None:
                self.notifier.alert(f"unhandled:{where}", f"Unhandled error in {where}: {msg}"[:400], now)
        except Exception:
            log.exception("could not write error to store")

    def stop(self) -> None:
        self._stop = True
        for r in self.runners:
            r.stop()

    # ---- coordinator ---------------------------------------------------------
    def reconcile(self) -> dict:
        now = self.now()
        res = reconcile_sleeves(self.runners, self.store, self.broker, now, self._log_error)
        if (res["orphans"] or res["missing"]) and self.notifier is not None:
            self.notifier.alert("reconcile", f"Reconcile: orphans flattened {res['orphans'] or 'none'}; "
                                             f"trades closed as missing {res['missing'] or 'none'}", now)
        return res

    def flatten_all(self, reason: str = "manual_flatten") -> int:
        """Every sleeve closes its positions; S0 runs last and adds the broker safety net."""
        orders = 0
        now = self.now()
        for r in self.runners[1:] + self.runners[:1]:
            try:
                orders += r.flatten(reason, now, safety_net=(r is self.runners[0]))
            except Exception as e:
                self._log_error(f"unhandled.{self.name(r)}", e)
        return orders

    # ---- overall heartbeat ---------------------------------------------------
    def write_overall(self) -> None:
        hbs = self.store.read_heartbeats()
        sleeves = [hbs.get(self.name(r)) for r in self.runners]
        states = [h["state"] for h in sleeves if h]
        state = next((s for s in ("running", "flattening", "sweeping", "preopen", "postclose") if s in states), "idle")
        cycle_ts = [h["last_cycle_ts"] for h in sleeves if h and h.get("last_cycle_ts")]
        fields = {"state": state, "open_positions": sum((h or {}).get("open_positions", 0) for h in sleeves)}
        if cycle_ts:
            fields["last_cycle_ts"] = max(cycle_ts, key=lambda s: datetime.fromisoformat(s))
        self.store.write_heartbeat("ALL", **fields)

    # ---- lifecycle -----------------------------------------------------------
    def startup(self) -> None:
        now = self.now()
        self.started_at = now
        self.store.write_heartbeat("ALL", state="idle", pid=os.getpid(), started_at=now)
        if self.notifier is not None:
            try:
                if self.clock.is_market_open():
                    self.notifier.alert("engine_restart", f"Engine (re)started during market hours at "
                                                          f"{now.strftime('%H:%M:%S')} ET (pid {os.getpid()}).", now)
            except Exception as e:
                self._log_error("startup.alert", e)
        for r in self.runners + self.jobs:
            fn = getattr(r, "startup", None)
            if fn is None:
                continue
            try:
                if r in self.runners:
                    fn(reconcile=False)
                else:
                    fn()
            except Exception as e:
                self._log_error(f"unhandled.{self.name(r)}", e)
        try:
            self.reconcile()
        except Exception as e:
            self._log_error("reconcile", e)
        self.write_overall()

    def run_forever(self) -> None:
        self.startup()
        while not self._stop:
            try:
                self.tick()
            except Exception as e:  # nothing should ever get here
                self._log_error("unhandled", e)
                self._sleep(5)

    def _check_flatten_request(self) -> bool:
        controls = self.store.read_controls()
        if not controls["flatten_requested"]:
            return False
        self.store.clear_flatten_request(self.now())
        log.info("flatten requested from dashboard: flattening every sleeve")
        self.flatten_all("manual_flatten")
        return True

    def plans(self, now: datetime) -> list[tuple[datetime, int, Optional[Callable], object]]:
        out = []
        for i, r in enumerate(self.runners + self.jobs):
            not_before = self._backoff.get(i)
            if not_before is not None and now < not_before:
                out.append((not_before, i, None, r))
                continue
            try:
                when, action = r.plan(now)
            except Exception as e:
                self._log_error(f"unhandled.{self.name(r)}", e)
                self._backoff[i] = now + timedelta(seconds=PLAN_RETRY_S)
                when, action = self._backoff[i], None
            out.append((when, i, action, r))
        return out

    def tick(self) -> None:
        """Ask every runner, wait for the earliest, run its action."""
        self._check_flatten_request()
        now = self.now()
        when, idx, action, runner = min(self.plans(now), key=lambda p: (p[0], p[1]))
        if not self._wait_until(when):
            return   # a flatten request interrupted the wait: re-plan
        if action is not None and not self._stop:
            try:
                action()
            except Exception as e:
                # back this runner off so an action that keeps failing cannot spin the loop
                self._log_error(f"unhandled.{self.name(runner)}", e)
                self._backoff[idx] = self.now() + timedelta(seconds=PLAN_RETRY_S)
            self.write_overall()

    def _wait_until(self, target: datetime) -> bool:
        """Sleep in chunks until target, refreshing the overall heartbeat (never a sleeve's last_cycle_ts).
        Returns False if a dashboard flatten request was served during the wait."""
        while not self._stop:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return True
            self._sleep(min(remaining, WAIT_CHUNK_S))
            self.write_overall()
            for r in self.runners:
                self.store.write_heartbeat(self.name(r), state=r.state)
            if self._check_flatten_request():
                return False
        return False
