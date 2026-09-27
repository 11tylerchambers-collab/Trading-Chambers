"""The $100 cash-account profile P100 (PHASE1A §8). Replay only, never trades.

Each night, the day's stored data is traded as a $100 cash account:

  - Long only. The strategy pool is S0's and S1's logic with P100's own params (profile `P100`), plus
    every intraday lab candidate currently meeting the §7 rule (L1, L3 variants). A short signal on SPY
    becomes a long in SH at the same moments, a short on QQQ a long in PSQ; other shorts cannot be
    expressed long-only and are dropped.
  - Shadow shorts: every short signal is also recorded as a simulated short on its own symbol, tagged
    `learning_only`. They are reported but never touch P100's cash, equity or sizing.
  - Cash settles next business day (T+1): the proceeds of a sale are unusable until then. Unsettled
    amounts carry across days in the ledger. Buying power = settled cash only.
  - Fractional shares, minimum order $1. Size = 1% of P100 equity at risk over the signal's own stop
    distance (as a fraction of price), capped by settled cash. One position per symbol at a time.
  - Costs: the replay cost model (0.02% spread, 0.01% × 2 slippage) plus a slippage buffer of 0.05% of
    notional per side (`P100_BUFFER`).
  - Equity carries forward day to day from $100. All pool strategies are intraday, so the account is
    flat every night and equity = settled + unsettled cash.

After the day's replay, P100 tunes its own S0 and S1 params with the one-step rule, scoring each
combination by its long-only (translated) net P&L on a fresh $100 over the recent sessions, and
stores them under profile P100, separate from the live sleeves.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .engine import trade_economics
from .replay import ReplayBroker, build_day, default_session, replay
from .risk import risk_qty
from .sleeve_replay import load_window, one_step as bar_one_step, replay_bars
from .store import Bar, Store
from .strategies import S1
from .strategy import Params
from .sweep import GRID as S0_GRID
from .sweep import grid_combos as s0_combos
from .sweep import one_step as s0_one_step

log = logging.getLogger("chambers.p100")

START_EQUITY = 100.0
RISK_PCT = 0.01
MIN_ORDER = 1.0
P100_BUFFER = 0.0005
INVERSE = {"SPY": "SH", "QQQ": "PSQ"}
TUNE_DAYS = 5
MIN_TUNE_TRADES = 20


@dataclass
class SigTrade:
    source: str
    symbol: str
    side: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    stop_frac: float                 # stop distance / entry price


@dataclass
class DayResult:
    date: date
    trades: list[dict] = field(default_factory=list)
    ledger: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# signal sources
# --------------------------------------------------------------------------

def p100_params(store: Store, sleeve: str, live_default: dict) -> dict:
    row = store.read_params(sleeve, "P100")
    if row is not None:
        return row["params"]
    live = store.read_params(sleeve, "LIVE")
    return live["params"] if live else live_default


def s0_trades(store: Store, d: date, params: Params, universe: list[str], session, source: str = "S0") -> list[SigTrade]:
    day = build_day(d, store.bars_for_day(d, symbols=universe), session, bool(store.econ_events_on(d)))
    res = replay(day, params, collect_hypothesis=False)
    return [SigTrade(source, t.symbol, t.side, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.exit_reason,
                     params.stop_pct / 100.0) for t in res.trades]


def bar_trades(spec, store: Store, d: date, params: dict, sessions: dict, source: str,
               window=None) -> list[SigTrade]:
    series, events, _ = window or load_window(spec, store, d, sessions, n=4)
    events = [e for e in events if e.day == d.isoformat()]
    res = replay_bars(spec, series, events, params, 20000.0)
    return [SigTrade(source, t.symbol, t.side, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.exit_reason,
                     (t.stop_distance / t.entry_price) if t.entry_price else 0.01) for t in res.trades]


def lab_trades(store: Store, d: date, universe: list[str], sessions: dict, session) -> list[SigTrade]:
    """Intraday lab candidates meeting §7's rule in the latest lab run."""
    from .lab import L1, l1_data
    out: list[SigTrade] = []
    for r in store.latest_lab_rule_passes():
        name, p = r["candidate"], r["params"] or {}
        try:
            if name == "L1_orb":
                series, events = l1_data(store, [d], sessions)
                res = replay_bars(L1, series, events, p, 20000.0)
                out += [SigTrade(name, t.symbol, t.side, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price,
                                 t.exit_reason, t.stop_distance / t.entry_price) for t in res.trades]
            elif name == "L3_vwap_idx":
                out += s0_trades(store, d, Params().replace(**p), ["SPY", "QQQ"], session, name)
            elif name == "L3_vwap_news":
                out += s0_trades(store, d, Params().replace(**p), universe, session, name)
            # L2 holds for several sessions; P100 is replayed one flat day at a time, so it is not in the pool
        except Exception as e:
            log.warning("P100: lab candidate %s skipped: %s", name, e)
    return out


# --------------------------------------------------------------------------
# the cash-account simulation
# --------------------------------------------------------------------------

class Prices:
    """Last 1-min close strictly before a moment's minute (what a live cycle at that moment would see)."""

    def __init__(self, bars: dict[str, list[Bar]]):
        self.ts = {s: [b.ts for b in bs] for s, bs in bars.items()}
        self.c = {s: [b.c for b in bs] for s, bs in bars.items()}

    def at(self, symbol: str, t: datetime) -> Optional[float]:
        ts = self.ts.get(symbol)
        if not ts:
            return None
        i = bisect.bisect_left(ts, t.replace(second=0, microsecond=0)) - 1
        return self.c[symbol][i] if i >= 0 else None


def next_business_day(d: date, sessions: Optional[dict] = None) -> date:
    later = sorted(x for x in (sessions or {}) if x > d)
    if later:
        return later[0]
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def translate(sig: SigTrade, prices: Prices) -> tuple[Optional[dict], Optional[str]]:
    """(long-only trade spec, skip reason)."""
    if sig.side == "long":
        return {"symbol": sig.symbol, "entry": sig.entry_price, "exit": sig.exit_price}, None
    inv = INVERSE.get(sig.symbol)
    if inv is None:
        return None, "short_not_translatable"
    e, x = prices.at(inv, sig.entry_ts), prices.at(inv, sig.exit_ts)
    if e is None or x is None:
        return None, "no_inverse_data"
    return {"symbol": inv, "entry": e, "exit": x}, None


def simulate(d: date, sigs: list[SigTrade], prices: Prices, start: dict, settle_to: date,
             news: tuple[bool, Optional[str]] = (False, None)) -> DayResult:
    """start: {'equity', 'settled', 'unsettled': [[amount, settle_date_iso], ...]} as of the day's open."""
    rb = ReplayBroker()
    settled = start["settled"]
    unsettled = [list(u) for u in start["unsettled"]]
    matured = [u for u in unsettled if u[1] <= d.isoformat()]
    settled += sum(u[0] for u in matured)
    unsettled = [u for u in unsettled if u[1] > d.isoformat()]
    settled_start = settled
    equity = start["equity"]
    res = DayResult(d)
    events = []                    # (ts, order, kind, index) — exits before entries at the same moment
    for i, s in enumerate(sigs):
        events.append((s.entry_ts, 1, "entry", i))
        events.append((s.exit_ts, 0, "exit", i))
    events.sort()
    open_: dict[int, dict] = {}
    held: set[str] = set()
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    net = 0.0
    for ts, _, kind, i in events:
        s = sigs[i]
        if kind == "entry":
            if s.side == "short":            # shadow short: learning only, never touches cash
                q, why = risk_qty(equity, RISK_PCT, s.stop_frac * s.entry_price, s.entry_price, 1.0, True, 1e-9)
                if not why:
                    bid, ask = rb.quote(s.entry_price)
                    xb, xa = rb.quote(s.exit_price)
                    g, c, n = trade_economics("short", q, s.entry_price, s.exit_price, bid, ask, xb, xa, P100_BUFFER)
                    res.trades.append({"source": s.source, "symbol": s.symbol, "signal_symbol": s.symbol,
                                       "side": "short", "qty": round(q, 8), "entry_ts": s.entry_ts,
                                       "entry_price": s.entry_price, "exit_ts": s.exit_ts, "exit_price": s.exit_price,
                                       "exit_reason": s.exit_reason, "gross_pnl": g, "est_cost": c, "net_pnl": n,
                                       "learning_only": True, "news_day": news[0], "event": news[1]})
            spec, why = translate(s, prices)
            if spec is None:
                skip(why)
                continue
            if spec["symbol"] in held:
                skip("already_holding")
                continue
            px = spec["entry"]
            q, why = risk_qty(equity, RISK_PCT, s.stop_frac * px, px, 1.0, True, 1e-9)
            if why:
                skip(why)
                continue
            notional = min(q * px, settled)
            if notional < MIN_ORDER:
                skip("no_settled_cash" if settled < MIN_ORDER else "below_min_order")
                continue
            q = int(notional / px * 1e8) / 1e8
            settled -= q * px
            held.add(spec["symbol"])
            open_[i] = {"spec": spec, "qty": q}
        else:
            o = open_.pop(i, None)
            if o is None:
                continue
            spec, q = o["spec"], o["qty"]
            bid, ask = rb.quote(spec["entry"])
            xb, xa = rb.quote(spec["exit"])
            g, c, n = trade_economics("long", q, spec["entry"], spec["exit"], bid, ask, xb, xa, P100_BUFFER)
            proceeds = q * spec["entry"] + n          # cash back = cost basis + net P&L
            unsettled.append([round(proceeds, 8), settle_to.isoformat()])
            held.discard(spec["symbol"])
            net += n
            res.trades.append({"source": s.source, "symbol": spec["symbol"], "signal_symbol": s.symbol, "side": "long",
                               "qty": q, "entry_ts": s.entry_ts, "entry_price": spec["entry"], "exit_ts": s.exit_ts,
                               "exit_price": spec["exit"], "exit_reason": s.exit_reason, "gross_pnl": g, "est_cost": c,
                               "net_pnl": n, "learning_only": False, "news_day": news[0], "event": news[1],
                               "detail": {"translated_from": s.symbol} if spec["symbol"] != s.symbol else None})
    real = [t for t in res.trades if not t["learning_only"]]
    shadow = [t for t in res.trades if t["learning_only"]]
    end_equity = settled + sum(u[0] for u in unsettled)
    res.ledger = {"start_equity": round(equity, 8), "end_equity": round(end_equity, 8),
                  "settled_cash_start": round(settled_start, 8), "settled_cash_end": round(settled, 8),
                  "unsettled": [[round(a, 8), sd] for a, sd in unsettled], "trades": len(real),
                  "skipped": sum(skipped.values()), "net_pnl": round(net, 8), "shadow_trades": len(shadow),
                  "shadow_net": round(sum(t["net_pnl"] for t in shadow), 8),
                  "detail": {"skipped": skipped, "matured": [[round(a, 8), sd] for a, sd in matured],
                             "sources": sorted({t["source"] for t in res.trades})}}
    return res


def start_state(store: Store, d: date) -> dict:
    prev = store.p100_last_ledger(before=d)
    if prev is None:
        return {"equity": START_EQUITY, "settled": START_EQUITY, "unsettled": []}
    return {"equity": prev["end_equity"], "settled": prev["settled_cash_end"], "unsettled": prev["unsettled"]}


def day_signals(store: Store, d: date, universe: list[str], sessions: dict, cfg_s0: dict, cfg_s1: dict,
                s0_p: Optional[dict] = None, s1_p: Optional[dict] = None, include_lab: bool = True) -> list[SigTrade]:
    session = sessions.get(d) or default_session(d)
    sigs = s0_trades(store, d, Params.from_dict(s0_p or p100_params(store, "S0", cfg_s0)), universe, session)
    sigs += bar_trades(S1, store, d, S1.params(s1_p or p100_params(store, "S1", cfg_s1)), sessions, "S1")
    if include_lab:
        sigs += lab_trades(store, d, universe, sessions, session)
    return sigs


def run_p100(store: Store, d: date, now: datetime, universe: list[str], sessions: Optional[dict] = None,
             cfg_s0: Optional[dict] = None, cfg_s1: Optional[dict] = None, tune: bool = True) -> DayResult:
    sessions = sessions or {}
    sigs = day_signals(store, d, universe, sessions, cfg_s0 or {}, cfg_s1 or {})
    extra = {s.symbol for s in sigs} | set(INVERSE.values())
    prices = Prices(store.bars_for_day(d, symbols=sorted(extra)))
    news = store.econ_events_on(d)
    res = simulate(d, sigs, prices, start_state(store, d), next_business_day(d, sessions),
                   (bool(news), "; ".join(e["event"] for e in news) or None))
    store.write_p100_day(d, res.ledger, res.trades)
    log.info("P100 %s: equity %.2f -> %.2f, %d trades, %d skipped, %d shadow shorts", d,
             res.ledger["start_equity"], res.ledger["end_equity"], res.ledger["trades"], res.ledger["skipped"],
             res.ledger["shadow_trades"])
    if tune:
        try:
            tune_p100(store, d, now, universe, sessions, cfg_s0 or {}, cfg_s1 or {})
        except Exception as e:
            log.exception("P100 tuning failed")
            store.log_error("p100.tune", f"{type(e).__name__}: {e}", None, now)
    return res


# --------------------------------------------------------------------------
# P100's own params (profile P100)
# --------------------------------------------------------------------------

def _score(sigs_by_day: dict, prices_by_day: dict, sessions: dict) -> tuple[float, int]:
    net, n = 0.0, 0
    for d, sigs in sigs_by_day.items():
        r = simulate(d, sigs, prices_by_day[d], {"equity": START_EQUITY, "settled": START_EQUITY, "unsettled": []},
                     next_business_day(d, sessions))
        net += r.ledger["net_pnl"]
        n += r.ledger["trades"]
    return net, n


def tune_p100(store: Store, d: date, now: datetime, universe: list[str], sessions: dict, cfg_s0: dict,
              cfg_s1: dict) -> dict:
    """One-step tuning of P100's S0 and S1 params on long-only translated results over the last sessions."""
    out = {}
    dates = [date.fromisoformat(x) for x in store.bar_dates(TUNE_DAYS, symbols=universe, before=d + timedelta(days=1))]
    prices_by_day = {x: Prices(store.bars_for_day(x, symbols=sorted(set(universe) | set(INVERSE.values()))))
                     for x in dates}
    # --- S0
    if not store.has_sweep_for(d, "S0", "P100"):
        cur = Params.from_dict(p100_params(store, "S0", cfg_s0))
        days = {x: build_day(x, store.bars_for_day(x, symbols=universe), sessions.get(x) or default_session(x),
                             bool(store.econ_events_on(x))) for x in dates}

        def s0_sigs(p: Params) -> dict:
            return {x: [SigTrade("S0", t.symbol, t.side, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price,
                                 t.exit_reason, p.stop_pct / 100.0) for t in replay(days[x], p, False).trades]
                    for x in dates}

        base_net, base_n = _score(s0_sigs(cur), prices_by_day, sessions)
        results = []
        for combo in s0_combos(cur):
            net, n = _score(s0_sigs(combo), prices_by_day, sessions)
            results.append({"params": {k: getattr(combo, k) for k in S0_GRID}, "net_pnl": round(net, 4), "trades": n,
                            "eligible": n >= MIN_TUNE_TRADES})
        out["S0"] = _decide(store, d, now, "S0", cur.to_dict(), results, base_n,
                            lambda best: s0_one_step(cur, cur.replace(**best)).to_dict(), S0_GRID)
    # --- S1
    if not store.has_sweep_for(d, "S1", "P100"):
        cur1 = S1.params(p100_params(store, "S1", cfg_s1))
        window = load_window(S1, store, d, sessions, n=TUNE_DAYS + 3)
        s1_dates = [x for x in dates if x.isoformat() in {e.day for e in window[1]}]
        prices1 = {x: prices_by_day.get(x) or Prices(store.bars_for_day(x, symbols=["SPY", "QQQ", "SH", "PSQ"]))
                   for x in s1_dates}

        def s1_sigs(p: dict) -> dict:
            return {x: bar_trades(S1, store, x, p, sessions, "S1", window) for x in s1_dates}

        base_net, base_n1 = _score(s1_sigs(cur1), prices1, sessions)
        results = []
        for combo in S1.grid_combos(cur1):
            net, n = _score(s1_sigs(combo), prices1, sessions)
            results.append({"params": {k: combo[k] for k in S1.grid}, "net_pnl": round(net, 4), "trades": n,
                            "eligible": n >= MIN_TUNE_TRADES})
        out["S1"] = _decide(store, d, now, "S1", cur1, results, base_n1,
                            lambda best: bar_one_step(S1, cur1, {**cur1, **best}), S1.grid)
    return out


def _decide(store: Store, d: date, now: datetime, sid: str, current: dict, results: list[dict], evidence: int,
            step, grid) -> dict:
    results.sort(key=lambda r: (-r["eligible"], -r["net_pnl"]))
    eligible = [r for r in results if r["eligible"]]
    summary = {"date": d.isoformat(), "profile": "P100", "sleeve_id": sid, "current": current,
               "evidence_trades": evidence, "combos_evaluated": len(results), "eligible": len(eligible),
               "best": eligible[0]["params"] if eligible else None, "chosen": current, "changed": False,
               "results": results[:20]}
    if evidence < MIN_TUNE_TRADES:
        summary["reason"] = f"insufficient_evidence: {evidence} P100 trades at current params (< {MIN_TUNE_TRADES})"
    elif not eligible:
        summary["reason"] = "no_eligible_combination"
    else:
        chosen = step(eligible[0]["params"])
        summary["chosen"] = chosen
        summary["changed"] = chosen != current
        summary["reason"] = "moved toward best" if summary["changed"] else "best_is_current"
    if summary["changed"]:
        store.write_params(summary["chosen"], "sweep", now, sid, "P100")
    elif store.read_params(sid, "P100") is None:
        store.write_params(current, "seed_from_live", now, sid, "P100")
    store.write_params_history(d, summary["chosen"], "sweep", summary, sid, "P100")
    return summary


def format_p100(res: DayResult) -> str:
    L = res.ledger
    lines = [f"P100 {res.date}: equity {L['start_equity']:.2f} -> {L['end_equity']:.2f} (net {L['net_pnl']:+.2f})",
             f"  settled cash {L['settled_cash_start']:.2f} at open -> {L['settled_cash_end']:.2f} at close; "
             f"unsettled carried: {L['unsettled']}",
             f"  matured this morning: {L['detail']['matured']}",
             f"  trades {L['trades']}, skipped {L['skipped']} {L['detail']['skipped']}",
             f"  shadow shorts (learning only, not in equity): {L['shadow_trades']} net {L['shadow_net']:+.2f}", ""]
    for t in res.trades:
        tag = " [learning_only]" if t["learning_only"] else ""
        via = f" (from {t['signal_symbol']} short)" if t["symbol"] != t["signal_symbol"] else ""
        lines.append(f"  {t['entry_ts'].strftime('%H:%M')}-{t['exit_ts'].strftime('%H:%M')} {t['source']:<12} "
                     f"{t['side']:<5} {t['symbol']:<5} x{t['qty']:.6f} {t['entry_price']:.4f}->{t['exit_price']:.4f} "
                     f"net {t['net_pnl']:+.4f} {t['exit_reason']}{via}{tag}")
    return "\n".join(lines)
