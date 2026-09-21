"""Entrypoint.

    python -m chambers.main             run the engine + dashboard (the service)
    python -m chambers.main --smoke     account, 3 AAPL bars, SPY quote
    python -m chambers.main --once      one live cycle now, then exit
    python -m chambers.main --replay YYYY-MM-DD
    python -m chambers.main --sweep     run the nightly sweep now
    python -m chambers.main --gate      Phase 0 acceptance checks 1-6
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from .clock import ET, MarketClock, now_et

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "chambers.db"
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"

log = logging.getLogger("chambers")


# --------------------------------------------------------------------------
# config / env
# --------------------------------------------------------------------------

def load_env(path: Path = ENV_PATH) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes. Never overrides
    variables already set in the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "strategy" not in cfg or "universe" not in cfg:
        raise SystemExit("config.yaml must contain 'strategy' and 'universe'")
    cfg["universe"] = [str(s).upper() for s in cfg["universe"]]
    return cfg


def require_paper_env() -> dict:
    """Fatal startup check. Returns the credentials. Exits with a message otherwise."""
    load_env()
    paper = os.environ.get("ALPACA_PAPER")
    if paper != "true":
        print(f"REFUSING TO START: ALPACA_PAPER must be exactly 'true' (got {paper!r}). "
              "Phase 0 never touches a live account.", file=sys.stderr)
        raise SystemExit(2)
    key, secret = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("REFUSING TO START: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (see .env.example).",
              file=sys.stderr)
        raise SystemExit(2)
    return {"api_key": key, "secret_key": secret, "paper": True,
            "dash_password": os.environ.get("DASH_PASSWORD", "")}


def setup_logging(to_file: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if to_file:
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(DATA_DIR / "logs" / "chambers.log",
                                                  maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_runtime(creds: dict):
    """Store + Broker + MarketClock wired together for live commands."""
    from .broker import Broker
    from .store import Store
    store = Store(DB_PATH)
    broker = Broker(creds["api_key"], creds["secret_key"], creds["paper"], store=store)
    clock = MarketClock(broker)
    return store, broker, clock


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_smoke() -> int:
    creds = require_paper_env()
    store, broker, clock = build_runtime(creds)
    print("== account ==")
    print(broker.account())
    c = broker.clock()
    print("== clock ==")
    print(c)
    # 3 bars for AAPL from the most recent session (today if a session day, else the last one)
    today = now_et().date()
    cal = broker.calendar(today - timedelta(days=7), today)
    last = [r for r in cal if r["open"] <= now_et()]
    if not last:
        print("no recent session in calendar")
        return 1
    s = last[-1]
    end = min(now_et(), s["close"])
    bars = broker.bars_1m(["AAPL"], s["open"], end)
    print(f"== 3 bars for AAPL ({s['date']}) ==")
    for b in (bars.get("AAPL") or [])[-3:]:
        print(b)
    if not bars.get("AAPL"):
        print("no AAPL bars returned")
    print("== quote for SPY ==")
    print(broker.quotes(["SPY"]).get("SPY"))
    return 0


def cmd_once() -> int:
    from .engine import Engine
    cfg = load_config()
    creds = require_paper_env()
    store, broker, clock = build_runtime(creds)
    eng = Engine(store, broker, clock, cfg["universe"], cfg["strategy"])
    eng.startup()
    result = eng.run_cycle()
    print(result)
    return 0


def optional_runtime():
    """(store, broker, clock) with a live broker when paper credentials exist, else (store, None, None).
    Used by the offline commands so replay/sweep/gate can use real session times when possible."""
    from .store import Store
    load_env()
    if os.environ.get("ALPACA_PAPER") == "true" and os.environ.get("ALPACA_API_KEY") \
            and os.environ.get("ALPACA_SECRET_KEY"):
        try:
            return build_runtime(require_paper_env())
        except Exception as e:  # keys present but unusable: fall back to offline
            log.warning("broker unavailable (%s); using default 9:30-16:00 sessions", e)
    return Store(DB_PATH), None, None


def sessions_for(clock, dates: list[str]) -> dict:
    """date -> Session for the given ISO dates, from the calendar when a clock is available."""
    if clock is None or not dates:
        return {}
    ds = sorted(date.fromisoformat(x) for x in dates)
    try:
        return clock.sessions_between(ds[0] - timedelta(days=1), ds[-1] + timedelta(days=1))
    except Exception as e:
        log.warning("calendar unavailable (%s); using default sessions", e)
        return {}


def current_params(store, cfg: dict):
    from .strategy import Params
    live = store.read_params()
    return Params.from_dict(live["params"] if live else cfg["strategy"])


def cmd_replay(day: str) -> int:
    from .replay import replay_day, format_replay
    cfg = load_config()
    store, broker, clock = optional_runtime()
    d = date.fromisoformat(day)
    sess = sessions_for(clock, [day]).get(d)
    res = replay_day(store, d, current_params(store, cfg), sess)
    print(format_replay(res))
    return 0


def cmd_sweep() -> int:
    from .sweep import run_sweep, N_DAYS
    cfg = load_config()
    store, broker, clock = optional_runtime()
    today = now_et().date()
    sessions = sessions_for(clock, store.bar_dates(N_DAYS))
    summary = run_sweep(store, current_params(store, cfg), today, now_et(), sessions=sessions)
    print(summary_text(summary))
    return 0


def summary_text(summary: dict) -> str:
    lines = [f"sweep {summary['date']}: {summary['reason']}",
             f"  days={summary['days']} combos={summary['combos_evaluated']} eligible={summary['eligible']}",
             f"  current={summary['current']}",
             f"  best={summary.get('best')} score={summary.get('best_score')}",
             f"  chosen={summary['chosen']}"]
    return "\n".join(lines)


def cmd_gate() -> int:
    from .gate import run_gate, format_gate
    cfg = load_config()
    store, broker, clock = optional_runtime()
    sessions = sessions_for(clock, store.cycle_dates(5))
    results = run_gate(store, sessions, universe_size=len(cfg["universe"]))
    print(format_gate(results))
    return 0 if all(r["pass"] for r in results["items"]) and len(results["sessions"]) >= 5 else 1


def cmd_run() -> int:
    """The service: engine loop in the main thread, dashboard in a daemon thread."""
    import threading
    from .engine import Engine
    from .dashboard.app import serve
    cfg = load_config()
    creds = require_paper_env()
    store, broker, clock = build_runtime(creds)
    eng = Engine(store, broker, clock, cfg["universe"], cfg["strategy"])
    t = threading.Thread(target=serve, kwargs={"db_path": DB_PATH, "password": creds["dash_password"],
                                               "broker": broker, "clock": clock, "universe": cfg["universe"],
                                               "host": "0.0.0.0", "port": 8080},
                         daemon=True, name="dashboard")
    t.start()
    eng.run_forever()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="chambers")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--replay", metavar="YYYY-MM-DD")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(to_file=not (args.smoke or args.replay or args.gate))
    if args.smoke:
        return cmd_smoke()
    if args.once:
        return cmd_once()
    if args.replay:
        return cmd_replay(args.replay)
    if args.sweep:
        return cmd_sweep()
    if args.gate:
        return cmd_gate()
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
