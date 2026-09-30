"""Night lab (PHASE1A §7): suggest only, never trades.

Runs after the evening report on session days as a separate process at lower CPU priority
(`python -m chambers.main --lab`, started by the engine with nice 10). It reads stored bars and writes
only `lab_runs`, `lab_results` and `lab_suggestions`. Before every replay batch it checks the engine's
recent cycle durations; if any cycle in the last 2 minutes took more than 20 s, it pauses (30 s at a
time) until they are back under.

Candidates, each replayed on stored bars with the same cost model and a random twin:
  L1_orb        opening range breakout, SPY/QQQ, 5-min bars
  L2_pullback   pullback in an uptrend, SPY/QQQ, session (daily) bars
  L3_vwap_idx   S0's VWAP reversion on S1's symbols (SPY/QQQ, 1-min)
  L3_vwap_news  S0 with skip_news_days = true (S0's universe, 1-min)

Suggestion rule, over the last ≥ 20 (up to 60) sessions: tune the params on the first half, grade the
tuned params on the second (unseen) half; suggest only if the graded net P&L after costs is > 0 AND
beats the candidate's twin on the same graded half. A suggestion waits for the user's approval on the
dashboard; approving only records the decision (wiring a candidate into a live sleeve is a separate
build).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Sequence

from .bars import TF_1D, atr, build_5m, sma
from .clock import ET, Session
from .replay import build_day, default_session, replay
from .sleeve_replay import Event, replay_bars
from .store import Bar, Store
from .strategies import COMMON_DEFAULTS, COMMON_TYPES, EntryEval, StrategySpec
from .strategy import Params
from .sweep import GRID as S0_GRID
from .sweep import grid_combos as s0_grid_combos
from .twin import day_seed, replay_twin_s0

log = logging.getLogger("chambers.lab")

MAX_SESSIONS = 60
MIN_SESSIONS = 20
PAUSE_ABOVE_MS = 20_000
PAUSE_STEP_S = 30.0
LAB_CAPITAL = 20000.0
LAB_SYMBOLS = ("SPY", "QQQ")


# ==========================================================================
# L1 — opening range breakout (5-min)
# ==========================================================================

def _same_day(hist: Sequence[Bar]) -> list[Bar]:
    d = hist[-1].ts.date()
    return [b for b in hist if b.ts.date() == d]


def l1_range(day: list[Bar], p: dict) -> Optional[tuple[float, float, datetime]]:
    if not day:
        return None
    o = day[0].ts.replace(hour=9, minute=30, second=0, microsecond=0)
    end = o + timedelta(minutes=p["range_minutes"])
    rng = [b for b in day if b.ts < end]
    if not rng:
        return None
    return max(b.h for b in rng), min(b.l for b in rng), end


def l1_entry(hist: Sequence[Bar], p: dict) -> EntryEval:
    price = hist[-1].c if hist else None
    day = _same_day(hist) if hist else []
    r = l1_range(day, p)
    if r is None or hist[-1].ts < r[2]:
        return EntryEval(None, "insufficient_bars", price)
    hi, lo, end = r
    height = hi - lo
    after = [b for b in day if b.ts >= end]
    long_stop = max(lo, price - height)
    detail = {"range_high": hi, "range_low": lo}
    sd = max(price - long_stop, 1e-9)
    if any(b.c > hi or b.c < lo for b in after[:-1]):
        return EntryEval(None, "already_broke_out", price, sd, detail)
    if price > hi:
        return EntryEval("long", "fired", price, price - max(lo, price - height), detail)
    if price < lo:
        return EntryEval("short", "fired", price, min(hi, price + height) - price, detail)
    return EntryEval(None, "inside_range", price, sd, detail)


def l1_exit(pos, hist: Sequence[Bar], p: dict) -> Optional[str]:
    c = hist[-1].c
    risk = abs(pos.entry_price - pos.stop_price) if pos.stop_price is not None else None
    if pos.stop_price is not None:
        if (pos.side == "long" and c <= pos.stop_price) or (pos.side == "short" and c >= pos.stop_price):
            return "stop_loss"
    if risk:
        tgt = pos.entry_price + p["target_r"] * risk * (1 if pos.side == "long" else -1)
        if (pos.side == "long" and c >= tgt) or (pos.side == "short" and c <= tgt):
            return "target"
    return None


L1 = StrategySpec(
    sleeve_id="L1_orb", name="Opening range breakout", strategy="orb_5m", timeframe="5m", symbols=LAB_SYMBOLS,
    allow_short=True, eod_flatten=True, fractional=False,
    defaults={"range_minutes": 60, "target_r": 2.0, **COMMON_DEFAULTS, "reentry_cooldown_bars": 0},
    types={"range_minutes": int, "target_r": float, **COMMON_TYPES},
    grid={"range_minutes": [30, 60, 90], "target_r": [1.5, 2.0, 3.0]},
    entry=l1_entry, exit=l1_exit, min_bars=13, expect="breakout runs to 2R before the stop", hist_bars=120)


# ==========================================================================
# L2 — pullback in an uptrend (session bars)
# ==========================================================================

def l2_entry(hist: Sequence[Bar], p: dict) -> EntryEval:
    price = hist[-1].c if hist else None
    closes = [b.c for b in hist]
    m = sma(closes, 200)
    a = atr(hist, 14)
    n = p["down_days"]
    if m is None or a is None or len(closes) < n + 1:
        return EntryEval(None, "insufficient_bars", price)
    detail = {"sma200": round(m, 4), "atr14": round(a, 4)}
    sd = p["stop_atr"] * a
    if price <= m:
        return EntryEval(None, "below_sma200", price, sd, detail)
    if not all(closes[-k] < closes[-k - 1] for k in range(1, n + 1)):
        return EntryEval(None, "no_pullback", price, sd, detail)
    return EntryEval("long", "fired", price, sd, detail)


def l2_exit(pos, hist: Sequence[Bar], p: dict) -> Optional[str]:
    c = hist[-1].c
    if len(hist) >= 2 and pos.bars_held >= 1 and c > hist[-2].c:
        return "up_close"
    if pos.stop_price is not None and c <= pos.stop_price:
        return "stop_loss"
    if pos.bars_held >= p["max_hold"]:
        return "time_stop"
    return None


L2 = StrategySpec(
    sleeve_id="L2_pullback", name="Pullback in uptrend", strategy="pullback_daily", timeframe="1d",
    symbols=LAB_SYMBOLS, allow_short=False, eod_flatten=False, fractional=False,
    defaults={"down_days": 3, "max_hold": 5, "stop_atr": 2.0, **COMMON_DEFAULTS, "reentry_cooldown_bars": 0},
    types={"down_days": int, "max_hold": int, "stop_atr": float, **COMMON_TYPES},
    grid={"down_days": [2, 3, 4], "max_hold": [3, 5, 8], "stop_atr": [1.5, 2.0, 3.0]},
    entry=l2_entry, exit=l2_exit, min_bars=201, expect="first up close after the pullback", hist_bars=260)


# ==========================================================================
# data
# ==========================================================================

def lab_sessions(store: Store, symbols: Sequence[str], sessions_map: Optional[dict], before: date,
                 n: int = MAX_SESSIONS) -> list[date]:
    return [date.fromisoformat(d) for d in store.bar_dates(n, symbols=list(symbols), before=before)]


def _sess(sessions_map, d) -> Session:
    return (sessions_map or {}).get(d) or default_session(d)


def l1_data(store: Store, dates: list[date], sessions_map) -> tuple[dict, list[Event]]:
    series = {s: [] for s in LAB_SYMBOLS}
    events: list[Event] = []
    for d in dates:
        sess = _sess(sessions_map, d)
        raw = store.bars_for_day(d, symbols=list(LAB_SYMBOLS))
        start = {s: len(series[s]) for s in series}
        last = {}
        for s in LAB_SYMBOLS:
            b5 = build_5m(raw.get(s, []), sess)
            series[s] += b5
            before = [b for b in raw.get(s, []) if b.ts < sess.flatten_at]
            if before:
                last[s] = before[-1].c
        by_t: dict = {}
        for s in series:
            for k in range(start[s], len(series[s])):
                t = min(series[s][k].ts + timedelta(minutes=5), sess.close) + timedelta(seconds=5)
                if t < sess.flatten_at:
                    by_t.setdefault(t, {})[s] = k
        day_ev = [Event(t, idx, sess.entries_allowed(t), day=d.isoformat()) for t, idx in sorted(by_t.items())]
        if day_ev:
            day_ev.append(Event(sess.flatten_at, {}, False, flatten_prices=last, day=d.isoformat()))
        events += day_ev
    return series, events


def l2_data(store: Store, dates: list[date]) -> tuple[dict, list[Event]]:
    if not dates:
        return {}, []
    end = datetime(dates[-1].year, dates[-1].month, dates[-1].day, tzinfo=ET) + timedelta(days=1)
    series = {s: store.bars_tf(s, TF_1D, None, end) for s in LAB_SYMBOLS}
    want = {d.isoformat() for d in dates}
    by_t: dict = {}
    for s, bars in series.items():
        for k, b in enumerate(bars):
            if b.ts.date().isoformat() in want:
                t = datetime(b.ts.year, b.ts.month, b.ts.day, 15, 55, tzinfo=ET)
                by_t.setdefault(t, {})[s] = k
    return series, [Event(t, idx, True, day=t.date().isoformat()) for t, idx in sorted(by_t.items())]


# ==========================================================================
# evaluation
# ==========================================================================

class Pauser:
    """Waits while the engine's cycles are slow (its cycle duration > 20 s in the last 2 minutes)."""

    def __init__(self, store: Store, now_fn: Callable[[], datetime], sleep_fn: Callable[[float], None] = time.sleep,
                 max_pause_s: float = 3600.0):
        self.store, self.now_fn, self.sleep_fn, self.max_pause_s = store, now_fn, sleep_fn, max_pause_s
        self.paused_s = 0.0

    def check(self) -> None:
        waited = 0.0
        while self.store.max_cycle_duration_since(self.now_fn() - timedelta(minutes=2)) > PAUSE_ABOVE_MS:
            if waited >= self.max_pause_s:
                log.warning("lab: engine cycles still slow after %.0f s of pausing; continuing", waited)
                return
            log.info("lab: engine cycle > 20 s; pausing")
            self.sleep_fn(PAUSE_STEP_S)
            waited += PAUSE_STEP_S
            self.paused_s += PAUSE_STEP_S


def split(dates: list) -> tuple[list, list]:
    h = len(dates) // 2
    return dates[:h], dates[h:]


def eval_bar_candidate(spec: StrategySpec, series: dict, events: list[Event], dates: list[date],
                       pauser: Pauser) -> dict:
    ds = [d.isoformat() for d in dates]
    train, grade = split(ds)
    tr_ev = [e for e in events if e.day in set(train)]
    gr_ev = [e for e in events if e.day in set(grade)]
    best, best_net, best_tr = spec.params(), None, 0
    for combo in spec.grid_combos(spec.params()):
        pauser.check()
        r = replay_bars(spec, series, tr_ev, combo, LAB_CAPITAL)
        if r.trades and (best_net is None or r.net_pnl > best_net):
            best, best_net, best_tr = combo, r.net_pnl, len(r.trades)
    pauser.check()
    g = replay_bars(spec, series, gr_ev, best, LAB_CAPITAL)
    p = g.fired / g.evaluated if g.evaluated else 0.0
    tw = replay_bars(spec, series, gr_ev, best, LAB_CAPITAL, twin_seed_for=lambda d: day_seed(f"LAB-{spec.sleeve_id}", d),
                     twin_p=p)
    return {"params": {k: best[k] for k in spec.grid}, "train_dates": train, "grade_dates": grade,
            "train_net": round(best_net or 0.0, 2), "train_trades": best_tr, "grade_net": round(g.net_pnl, 2),
            "grade_trades": len(g.trades), "twin_grade_net": round(tw.twin_net, 2), "twin_p": round(p, 6),
            "twin_seed": day_seed(f"LAB-{spec.sleeve_id}", grade[0]) if grade else None}


def eval_s0_candidate(name: str, store: Store, symbols: list[str], dates: list[date], sessions_map, pauser: Pauser,
                      skip_news: bool) -> dict:
    days = [build_day(d, store.bars_for_day(d, symbols=symbols), _sess(sessions_map, d),
                      bool(store.econ_events_on(d))) for d in dates]
    train, grade = split(days)
    base = Params(skip_news_days=skip_news)
    best, best_net, best_tr = base, None, 0
    for combo in s0_grid_combos(base):
        pauser.check()
        rs = [replay(dd, combo, collect_hypothesis=False) for dd in train]
        net, n = sum(r.net_pnl for r in rs), sum(len(r.trades) for r in rs)
        if n and (best_net is None or net > best_net):
            best, best_net, best_tr = combo, net, n
    pauser.check()
    gr = [replay(dd, best, collect_hypothesis=False) for dd in grade]
    g_net = sum(r.net_pnl for r in gr)
    ev = sum(r.signals_evaluated for r in gr)
    p = sum(r.signals_fired for r in gr) / ev if ev else 0.0
    tw_net = sum(replay_twin_s0(dd, best, day_seed(f"LAB-{name}", dd.date.isoformat()), p).net_pnl for dd in grade)
    return {"params": {k: getattr(best, k) for k in (*S0_GRID, "skip_news_days")},
            "train_dates": [d.date.isoformat() for d in train], "grade_dates": [d.date.isoformat() for d in grade],
            "train_net": round(best_net or 0.0, 2), "train_trades": best_tr, "grade_net": round(g_net, 2),
            "grade_trades": sum(len(r.trades) for r in gr), "twin_grade_net": round(tw_net, 2), "twin_p": round(p, 6),
            "twin_seed": day_seed(f"LAB-{name}", grade[0].date.isoformat()) if grade else None}


def meets_rule(r: dict, n_sessions: int) -> tuple[bool, str]:
    if n_sessions < MIN_SESSIONS:
        return False, f"insufficient_sessions: {n_sessions} < {MIN_SESSIONS}"
    if r["grade_net"] <= 0:
        return False, f"graded half net {r['grade_net']:+.2f} <= 0"
    if r["grade_net"] <= r["twin_grade_net"]:
        return False, f"graded half net {r['grade_net']:+.2f} does not beat its twin {r['twin_grade_net']:+.2f}"
    return True, f"graded half net {r['grade_net']:+.2f} > 0 and beats its twin {r['twin_grade_net']:+.2f}"


def run_lab(store: Store, now: datetime, universe: list[str], sessions_map: Optional[dict] = None,
            sleep_fn: Callable[[float], None] = time.sleep, now_fn: Optional[Callable[[], datetime]] = None,
            max_sessions: int = MAX_SESSIONS) -> dict:
    now_fn = now_fn or (lambda: datetime.now(ET))
    pauser = Pauser(store, now_fn, sleep_fn)
    before = now.date() + timedelta(days=1)
    idx_dates = lab_sessions(store, LAB_SYMBOLS, sessions_map, before, max_sessions)
    s0_dates = lab_sessions(store, universe, sessions_map, before, max_sessions)
    run_id = store.start_lab_run(now, [d.isoformat() for d in idx_dates])
    t0 = time.monotonic()
    out = {"run_id": run_id, "results": [], "suggestions": []}
    candidates: list[tuple[str, Callable[[], tuple[dict, int]]]] = [
        ("L1_orb", lambda: (eval_bar_candidate(L1, *l1_data(store, idx_dates, sessions_map), idx_dates, pauser),
                            len(idx_dates))),
        ("L2_pullback", lambda: (eval_bar_candidate(L2, *l2_data(store, idx_dates), idx_dates, pauser),
                                 len(idx_dates))),
        ("L3_vwap_idx", lambda: (eval_s0_candidate("L3_vwap_idx", store, list(LAB_SYMBOLS), idx_dates, sessions_map,
                                                   pauser, False), len(idx_dates))),
        ("L3_vwap_news", lambda: (eval_s0_candidate("L3_vwap_news", store, universe, s0_dates, sessions_map,
                                                    pauser, True), len(s0_dates))),
    ]
    status = "done"
    for name, fn in candidates:
        try:
            r, n = fn()
            ok, reason = meets_rule(r, n)
        except Exception as e:
            log.exception("lab candidate %s failed", name)
            r, n, ok, reason = {"params": None, "train_dates": [], "grade_dates": [], "train_net": None,
                                "grade_net": None, "grade_trades": None, "twin_grade_net": None,
                                "twin_seed": None}, 0, False, f"error: {type(e).__name__}: {e}"
            status = "done_with_errors"
        rid = store.write_lab_result(run_id, name, n, r["train_dates"], r["grade_dates"], r["params"], r["train_net"],
                                     r["grade_net"], r["grade_trades"], r["twin_grade_net"], r["twin_seed"], ok,
                                     reason, {k: r.get(k) for k in ("train_trades", "twin_p")})
        row = {"candidate": name, "sessions": n, "meets_rule": ok, "reason": reason, **r}
        out["results"].append(row)
        if ok:
            store.write_lab_suggestion(run_id, rid, name, now_fn(), r["params"], n, r["grade_net"],
                                       r["twin_grade_net"], r["grade_trades"])
            out["suggestions"].append(name)
    store.finish_lab_run(run_id, now_fn(), status, pauser.paused_s,
                         {"duration_s": round(time.monotonic() - t0, 1), "suggestions": out["suggestions"]})
    out["paused_s"] = pauser.paused_s
    out["duration_s"] = round(time.monotonic() - t0, 1)
    return out


def format_lab(out: dict) -> str:
    lines = [f"night lab run {out['run_id']}  ({out.get('duration_s')} s, paused {out.get('paused_s', 0):.0f} s)"]
    for r in out["results"]:
        lines.append(f"- {r['candidate']}: sessions={r['sessions']} tuned={r.get('params')}")
        if r.get("grade_net") is not None:
            lines.append(f"    train net {r['train_net']:+.2f} ({r.get('train_trades')} tr) | graded net "
                         f"{r['grade_net']:+.2f} ({r['grade_trades']} tr) | twin {r['twin_grade_net']:+.2f}")
        lines.append(f"    {'SUGGEST' if r['meets_rule'] else 'no'}: {r['reason']}")
    lines.append("suggestions: " + (", ".join(out["suggestions"]) or "none"))
    return "\n".join(lines)


def launch_lab(python: str, cwd: str) -> int:
    """Start `--lab` as a separate process at nice 10 (below-normal priority on Windows). Returns its pid."""
    import os
    import subprocess
    import sys
    kw: dict = {"cwd": cwd, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kw["preexec_fn"] = lambda: os.nice(10)
        kw["start_new_session"] = True
    else:
        kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    p = subprocess.Popen([python or sys.executable, "-m", "chambers.main", "--lab"], **kw)
    return p.pid
