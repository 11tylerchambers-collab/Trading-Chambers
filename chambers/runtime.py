"""Wiring for the Phase 1A service: every active sleeve, their twins, the shared portfolio risk, the
scheduler and its jobs. Used by `python -m chambers.main` (the service) and by `--once`, so both see
the same set of sleeves (reconcile must always cover every sleeve: the broker holds their sum).
"""
from __future__ import annotations

import logging
import time
from datetime import timezone
from typing import Callable, Optional

from .engine import Engine
from .risk import Portfolio
from .scheduler import Scheduler
from .sleeve import BarSleeve, make_sleeve
from .strategies import SPECS
from .twin import LiveTwin

log = logging.getLogger("chambers.runtime")

SLEEVE_META = {
    "S0": ("VWAP reversion (Phase 0)", "vwap_reversion_1m", "1m"),
    "S1": (SPECS["S1"].name, SPECS["S1"].strategy, SPECS["S1"].timeframe),
    "S2": (SPECS["S2"].name, SPECS["S2"].strategy, SPECS["S2"].timeframe),
    "S3": (SPECS["S3"].name, SPECS["S3"].strategy, SPECS["S3"].timeframe),
}


def sleeve_cfg(cfg: dict, sid: str) -> dict:
    return (cfg.get("sleeves") or {}).get(sid) or {}


def active_sleeves(cfg: dict) -> list[str]:
    return [sid for sid in ("S0", "S1", "S2", "S3") if sleeve_cfg(cfg, sid).get("active", True)]


def register_sleeves(store, cfg: dict) -> None:
    """Keep the `sleeves` table in step with config.yaml."""
    for sid in ("S0", "S1", "S2", "S3"):
        sc = sleeve_cfg(cfg, sid)
        name, strategy, tf = SLEEVE_META[sid]
        symbols = list(cfg["universe"]) if sid == "S0" else list(SPECS[sid].symbols)
        store.upsert_sleeve(sid, name, strategy, symbols, tf, float(sc.get("capital", 20000)),
                            bool(sc.get("active", True)))


def build_engine(cfg: dict, store, broker, clock, sleep_fn: Callable[[float], None] = time.sleep,
                 notifier=None, jobs: Optional[list] = None, fetch_history: bool = True) -> tuple[Scheduler, dict]:
    """(scheduler, {sleeve_id: runner}). S0 is always first so it runs first when cycles coincide."""
    register_sleeves(store, cfg)
    risk = cfg.get("risk") or {}
    portfolio = Portfolio(store, broker, clock, float(risk.get("daily_loss_pct", 0.02)),
                          int(risk.get("max_same_side", 15)), notifier=notifier)
    runners: dict = {}
    active = active_sleeves(cfg)
    s0 = Engine(store, broker, clock, cfg["universe"], cfg["strategy"], sleep_fn=sleep_fn)
    runners["S0"] = s0          # S0 always runs (the Phase 0 engine); `active` applies to S1–S3
    for sid in ("S1", "S2", "S3"):
        if sid in active:
            runners[sid] = make_sleeve(sid, store, broker, clock, cfg, sleep_fn, fetch_history=fetch_history)
    for sid, r in runners.items():
        r.portfolio = portfolio
        if isinstance(r, Engine):
            r.twin = LiveTwin(r, allow_short=True, fallback_rate=lambda e=r: s0_fallback_rate(e))
        else:
            day_fn = (lambda now: now.astimezone(timezone.utc).date().isoformat()) if sid == "S2" else None
            r.twin = LiveTwin(r, allow_short=r.spec.allow_short, fee_rate=r.fee_rate,
                              fallback_rate=r.fallback_rate, day_fn=day_fn)
    sched = Scheduler(store, broker, clock, list(runners.values()), jobs=jobs or [], sleep_fn=sleep_fn,
                      notifier=notifier)
    sched.portfolio = portfolio
    return sched, runners


def s0_fallback_rate(eng: Engine) -> Optional[float]:
    """S0's twin p with no live signal history: the replay firing rate on the latest stored day."""
    from datetime import date
    from .replay import replay_day
    dates = eng.store.bar_dates(1, symbols=eng.universe, before=eng.now().date())
    if not dates:
        return None
    r = replay_day(eng.store, date.fromisoformat(dates[0]), eng.params, symbols=eng.universe)
    return r.signals_fired / r.signals_evaluated if r.signals_evaluated else None


# --------------------------------------------------------------------------
# scheduled jobs
# --------------------------------------------------------------------------

LAB_SYMBOLS = ["SPY", "QQQ"]
LAB_SESSIONS = 60
DAILY_SYMBOLS = ["SPY", "QQQ", "GLD", "USO"]
DAILY_HISTORY = 260


def nightly_history(store, broker, clock, cfg: dict, d, now) -> None:
    """The day's 1-min bars for P100's inverse ETFs, plus the night lab's history (engine = single writer)."""
    from datetime import timedelta
    from . import history
    extra = list((cfg.get("p100") or {}).get("extra_symbols") or ["SH", "PSQ"])
    history.ensure_1m(store, broker, clock, extra, 5, d + timedelta(days=1))
    history.ensure_1m(store, broker, clock, LAB_SYMBOLS, LAB_SESSIONS, d + timedelta(days=1))
    history.ensure_daily(store, broker, DAILY_SYMBOLS, DAILY_HISTORY, now)


def build_jobs(cfg: dict, store, broker, clock, runners: dict, notifier=None, backup_dir=None,
               evening=None, p100=None, lab=None, morning=None) -> list:
    """The nightly chain (+ the morning brief when `morning` is given). `evening`, `p100` and `lab` are
    callables (d, now) supplied by main; a missing one is simply not scheduled."""
    from pathlib import Path
    from .jobs import MorningBrief, NightlyJobs, Step
    from .recon import backup_exists, nightly_backup, run_recon
    fee = float((cfg.get("crypto") or {}).get("fee_rate", 0.0025))
    bdir = Path(backup_dir) if backup_dir else Path(store.path).parent / "backups"
    steps = [Step("history", lambda d, now: nightly_history(store, broker, clock, cfg, d, now))]
    if p100 is not None:
        steps.append(Step("p100", p100, done=lambda d: store.p100_ledger_for(d) is not None))
    steps.append(Step("recon", lambda d, now: run_recon(store, broker, now, "equity_session", fee, notifier),
                      done=lambda d: any(r["kind"] == "equity_session" and r["status"] != "error"
                                         for r in store.recon_for_day(d))))
    steps.append(Step("backup", lambda d, now: nightly_backup(store, bdir, d), done=lambda d: backup_exists(bdir, d)))
    if evening is not None:
        steps.append(Step("evening", evening, done=lambda d: any(
            a["ts"][:10] == d.isoformat() and a["status"] in ("sent", "disabled", "dry_run")
            for a in store.recent_alerts(20, kind="evening"))))
    if lab is not None:
        steps.append(Step("lab", lab, done=lambda d: any((r["started_at"] or "")[:10] == d.isoformat()
                                                        for r in store.lab_runs(5))))
    waits = [sid for sid in ("S0", "S1", "S3") if sid in runners]
    jobs: list = [NightlyJobs(store, clock, steps, waits)]
    if morning is not None:
        jobs.append(MorningBrief(store, clock, morning))
    s2 = runners.get("S2")
    if s2 is not None:
        s2.after_rollover = lambda now: run_recon(store, broker, now, "crypto_rollover", fee, notifier)
    return jobs
