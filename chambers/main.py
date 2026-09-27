"""Entrypoint.

    python -m chambers.main             run the engine + dashboard (the service)
    python -m chambers.main --smoke     account, 3 AAPL bars, SPY quote
    python -m chambers.main --once      one live cycle now, then exit
    python -m chambers.main --replay YYYY-MM-DD [--params k=v,k=v]
    python -m chambers.main --recompute-costs   re-cost every closed live trade (live_trade_economics)
    python -m chambers.main --sweep     run the nightly sweep now
    python -m chambers.main --gate      Phase 0 acceptance checks 1-6

Phase 1A: --once, --replay and --sweep take --sleeve S0|S1|S2|S3 (default S0).
    python -m chambers.main --brief morning|evening [--dry-run] [--date YYYY-MM-DD]
                                        build (and send, unless --dry-run) a Telegram message
    python -m chambers.main --test-alert    send one test alert
    python -m chambers.main --watchdog      alert if any sleeve's heartbeat is > 3 min stale (run by a timer)
    python -m chambers.main --lab           the night lab (normally started by the engine at nice 10)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

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
    if cfg.get("data_feed", "iex") not in ("iex", "sip"):
        raise SystemExit("config.yaml: data_feed must be 'iex' or 'sip'")
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


def build_runtime(creds: dict, cfg: Optional[dict] = None):
    """Store + Broker + MarketClock wired together for live commands."""
    from .broker import Broker
    from .store import Store
    store = Store(DB_PATH)
    if store.migration_backup:
        print(f"Phase 1A migration: Phase 0 database backed up to {store.migration_backup}")
    feed = (cfg or {}).get("data_feed") or _config_feed()
    broker = Broker(creds["api_key"], creds["secret_key"], creds["paper"], store=store, data_feed=feed)
    clock = MarketClock(broker)
    return store, broker, clock


def _config_feed() -> str:
    try:
        return load_config().get("data_feed", "iex")
    except Exception:
        return "iex"


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


def cmd_once(sleeve: str = "S0") -> int:
    """One live cycle of one sleeve now. Every sleeve is started and reconciled first: reconciling one
    sleeve alone would treat the others' broker positions as orphans and flatten them."""
    from .runtime import build_engine
    cfg = load_config()
    creds = require_paper_env()
    store, broker, clock = build_runtime(creds, cfg)
    sched, runners = build_engine(cfg, store, broker, clock)
    if sleeve not in runners:
        print(f"sleeve {sleeve} is not active (config.yaml sleeves.{sleeve}.active)", file=sys.stderr)
        return 2
    sched.startup()
    result = runners[sleeve].run_cycle()
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


def apply_param_overrides(base, text: Optional[str]):
    """`entry_dev_pct=0.30,vol_mult=1.5` → base Params with those keys replaced. Unknown keys raise."""
    from .strategy import PARAM_KEYS
    if not text:
        return base
    overrides = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or key not in PARAM_KEYS:
            raise ValueError(f"bad --params entry {part!r}; expected key=value with key in {', '.join(PARAM_KEYS)}")
        overrides[key] = value.strip()
    return base.replace(**overrides)


def cmd_replay(day: str, params_text: Optional[str] = None, sleeve: str = "S0") -> int:
    from .replay import replay_day, format_replay
    cfg = load_config()
    store, broker, clock = optional_runtime()
    d = date.fromisoformat(day)
    sess = sessions_for(clock, [day]).get(d)
    if sleeve == "S0":
        params = apply_param_overrides(current_params(store, cfg), params_text)
        res = replay_day(store, d, params, sess, symbols=cfg["universe"])
        print(format_replay(res))
        print(twin_line(store, d, params, sess, cfg["universe"], res.net_pnl))
        return 0
    return cmd_replay_sleeve(store, clock, cfg, sleeve, d, params_text)


def twin_line(store, d: date, params, sess, universe: list[str], sleeve_net: float) -> str:
    """S0's random twin for the day, from the logged seed and p (reproduces the live twin)."""
    from .replay import build_day
    from .twin import day_seed, replay_twin_s0, trailing_rate
    row = store.get_twin_seed("S0", d.isoformat())
    seed, p, src = (row["seed"], row["p"], "logged") if row else \
        (day_seed("S0", d.isoformat()), trailing_rate(store, "S0", d.isoformat())[0], "computed")
    tw = replay_twin_s0(build_day(d, store.bars_for_day(d, symbols=universe), sess), params, seed, p)
    return (f"twin ({src} seed={seed} p={p:.5f}): trades={len(tw.trades)} net={tw.net_pnl:.2f}  "
            f"edge vs twin={sleeve_net - tw.net_pnl:.2f}")


def sleeve_params(store, cfg: dict, sleeve: str, text: Optional[str] = None) -> dict:
    from .strategies import SPECS
    spec = SPECS[sleeve]
    live = store.read_params(sleeve)
    base = spec.params(live["params"] if live else ((cfg.get("sleeves") or {}).get(sleeve) or {}).get("params") or {})
    if text:
        over = {}
        for part in text.split(","):
            if not part.strip():
                continue
            k, sep, v = part.partition("=")
            if not sep or k.strip() not in spec.types:
                raise ValueError(f"bad --params entry {part!r}; keys for {sleeve}: {', '.join(spec.types)}")
            over[k.strip()] = v.strip()
        base = spec.params({**base, **over})
    return base


def cmd_replay_sleeve(store, clock, cfg: dict, sleeve: str, d: date, params_text: Optional[str]) -> int:
    """Replay one day of S1/S2/S3 from stored bars, with the day's random twin."""
    from .sleeve_replay import format_bar_replay, load_window, replay_bars
    from .strategies import SPECS
    from .twin import day_seed, trailing_rate
    spec = SPECS.get(sleeve)
    if spec is None:
        print(f"unknown sleeve {sleeve!r}", file=sys.stderr)
        return 2
    sessions = {}
    if clock is not None:
        try:
            sessions = clock.sessions_between(d - timedelta(days=200), d)
        except Exception as e:
            log.warning("calendar unavailable (%s)", e)
    n = {"S1": 4, "S2": 10, "S3": 120}[sleeve]
    series, events, _ = load_window(spec, store, d, sessions, n=n)
    events = [e for e in events if e.day == d.isoformat()]
    p = sleeve_params(store, cfg, sleeve, params_text)
    capital = store.sleeve_equity(sleeve) or float(((cfg.get("sleeves") or {}).get(sleeve) or {}).get("capital", 20000))
    fee = float((cfg.get("crypto") or {}).get("fee_rate", 0.0025)) if spec.fractional else 0.0
    seed_row = store.get_twin_seed(sleeve, d.isoformat())
    twin_p = seed_row["p"] if seed_row else trailing_rate(store, sleeve, d.isoformat())[0]
    res = replay_bars(spec, series, events, p, capital, fee, twin_seed_for=lambda day: day_seed(sleeve, day),
                      twin_p=twin_p)
    print(format_bar_replay(spec, res, d.isoformat()))
    return 0


def recompute_costs(store) -> tuple[int, float, float]:
    """Re-cost every closed live trade with `live_trade_economics`. Returns (trades, old net sum, new net sum)."""
    from .engine import live_trade_economics
    old = new = 0.0
    trades = store.all_closed_trades()
    for t in trades:
        gross, cost, net = live_trade_economics(t.side, t.qty, t.entry_price, t.exit_price,
                                                t.entry_bid, t.entry_ask, t.exit_bid, t.exit_ask)
        store.update_trade_economics(t.id, gross, cost, net)
        old += t.net_pnl or 0.0
        new += net
    return len(trades), old, new


def cmd_recompute_costs() -> int:
    from .store import Store
    n, old, new = recompute_costs(Store(DB_PATH))
    print(f"recomputed {n} closed trades: net_pnl sum {old:.2f} -> {new:.2f}")
    return 0


def cmd_sweep(sleeve: str = "S0") -> int:
    from .sweep import run_sweep, N_DAYS
    cfg = load_config()
    store, broker, clock = optional_runtime()
    today = now_et().date()
    if sleeve != "S0":
        from .sleeve_replay import run_bar_sweep
        from .strategies import SPECS
        spec = SPECS[sleeve]
        sessions = {}
        if clock is not None:
            try:
                sessions = clock.sessions_between(today - timedelta(days=200), today)
            except Exception as e:
                log.warning("calendar unavailable (%s)", e)
        capital = store.sleeve_equity(sleeve) or 20000.0
        fee = float((cfg.get("crypto") or {}).get("fee_rate", 0.0025)) if spec.fractional else 0.0
        s = run_bar_sweep(store, spec, sleeve_params(store, cfg, sleeve), today, now_et(), capital, fee, sessions)
        print(f"sweep {sleeve} {s['date']}: {s['reason']}\n  days={len(s.get('days', []))} combos={s.get('combos_evaluated')}"
              f" eligible={s.get('eligible')} evidence_trades={s.get('evidence_trades')}\n  best={s.get('best')} "
              f"score={s.get('best_score')}\n  chosen={s['chosen']}")
        return 0
    sessions = sessions_for(clock, store.bar_dates(N_DAYS, symbols=cfg["universe"]))
    summary = run_sweep(store, current_params(store, cfg), today, now_et(), sessions=sessions,
                        symbols=cfg["universe"])
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
    from .dashboard.app import serve
    from .notify import Notifier
    from .runtime import build_engine, build_jobs
    cfg = load_config()
    creds = require_paper_env()
    store, broker, clock = build_runtime(creds, cfg)
    notifier = Notifier.from_env(store)
    sched, runners = build_engine(cfg, store, broker, clock, notifier=notifier)
    sched.jobs = build_jobs(cfg, store, broker, clock, runners, notifier=notifier, backup_dir=DATA_DIR / "backups",
                            evening=lambda d, now: send_evening(store, broker, notifier, d, now),
                            lab=lambda d, now: start_lab(),
                            morning=lambda now: send_morning(store, broker, clock, notifier, now))
    t = threading.Thread(target=serve, kwargs={"db_path": DB_PATH, "password": creds["dash_password"],
                                               "broker": broker, "clock": clock, "universe": cfg["universe"],
                                               "host": "0.0.0.0", "port": 8080},
                         daemon=True, name="dashboard")
    t.start()
    sched.run_forever()
    return 0


def account_equity(broker) -> Optional[float]:
    try:
        return float(broker.account()["equity"]) if broker is not None else None
    except Exception:
        return None


def send_evening(store, broker, notifier, d: date, now: datetime) -> str:
    from .messages import evening_report
    return notifier.send("evening", "evening", evening_report(store, d, account_equity(broker)), now)


def send_morning(store, broker, clock, notifier, now: datetime) -> str:
    from .messages import morning_brief
    return notifier.send("morning", "morning", morning_brief(store, broker, clock, now), now)


def cmd_brief(which: str, dry_run: bool, day: Optional[str]) -> int:
    from .notify import Notifier
    store, broker, clock = optional_runtime()
    notifier = Notifier.from_env(store, dry_run=dry_run)
    now = now_et()
    if which == "morning":
        status = send_morning(store, broker, clock, notifier, now)
    else:
        status = send_evening(store, broker, notifier, date.fromisoformat(day) if day else now.date(), now)
    if not dry_run:
        print(f"evening/morning message: {status}")
    return 0


def cmd_test_alert() -> int:
    from .notify import Notifier
    from .store import Store
    load_env()
    store = Store(DB_PATH)
    n = Notifier.from_env(store)
    status = n.send("test", "test", f"Trading Chambers test alert {now_et().strftime('%Y-%m-%d %H:%M:%S')} ET. "
                                    "If you can read this on your phone, alerts work.")
    print(f"test alert: {status}")
    return 0 if status == "sent" else 1


def start_lab() -> int:
    from .lab import launch_lab
    pid = launch_lab(sys.executable, str(ROOT))
    log.info("night lab started (pid %d, nice 10)", pid)
    return pid


def cmd_lab() -> int:
    from .lab import format_lab, run_lab
    if hasattr(os, "nice"):
        try:
            cur = os.nice(0)
            if cur < 10:
                os.nice(10 - cur)          # lower CPU priority even when started by hand
        except OSError:
            pass
    cfg = load_config()
    store, broker, clock = optional_runtime()
    now = now_et()
    sessions = {}
    if clock is not None:
        try:
            sessions = clock.sessions_between(now.date() - timedelta(days=120), now.date())
        except Exception as e:
            log.warning("calendar unavailable (%s); assuming 9:30-16:00", e)
    out = run_lab(store, now, cfg["universe"], sessions)
    print(format_lab(out))
    return 0


def cmd_watchdog() -> int:
    from .notify import Notifier
    from .runtime import active_sleeves
    from .watchdog import run_watchdog
    cfg = load_config()
    store, broker, clock = optional_runtime()
    if clock is None:
        print("watchdog needs Alpaca credentials for the market calendar", file=sys.stderr)
        return 2
    stale = run_watchdog(store, clock, Notifier.from_env(store), active_sleeves(cfg))
    for s in stale:
        print(f"STALE {s['sleeve_id']}: due {s['due'].isoformat()} last {s['last_cycle_ts']}")
    return 1 if stale else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="chambers")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--replay", metavar="YYYY-MM-DD")
    ap.add_argument("--params", metavar="k=v,k=v", help="with --replay: override the live params")
    ap.add_argument("--sleeve", default="S0", help="with --replay/--sweep: which sleeve (default S0)")
    ap.add_argument("--recompute-costs", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--brief", choices=["morning", "evening"])
    ap.add_argument("--dry-run", action="store_true", help="with --brief: print the text, send nothing")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="with --brief evening: which day")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--lab", action="store_true")
    args = ap.parse_args(argv)
    if args.params and not args.replay:
        ap.error("--params only applies to --replay")
    setup_logging(to_file=not (args.smoke or args.replay or args.gate or args.recompute_costs or args.brief
                                or args.test_alert or args.watchdog))
    if args.brief:
        return cmd_brief(args.brief, args.dry_run, args.date)
    if args.test_alert:
        return cmd_test_alert()
    if args.watchdog:
        return cmd_watchdog()
    if args.lab:
        return cmd_lab()
    if args.smoke:
        return cmd_smoke()
    if args.once:
        return cmd_once(args.sleeve)
    if args.replay:
        from .strategy import Params
        try:
            if args.sleeve == "S0":
                apply_param_overrides(Params(), args.params)   # validate before touching the DB
            else:
                from .strategies import SPECS
                if args.sleeve not in SPECS:
                    ap.error(f"unknown sleeve {args.sleeve}")
        except ValueError as e:
            ap.error(str(e))
        return cmd_replay(args.replay, args.params, args.sleeve)
    if args.recompute_costs:
        return cmd_recompute_costs()
    if args.sweep:
        return cmd_sweep(args.sleeve)
    if args.gate:
        return cmd_gate()
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
