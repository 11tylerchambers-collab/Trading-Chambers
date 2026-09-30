"""Text of the morning brief and the evening report (PHASE1A §6). Plain text, short lines, no tables:
they are read on a phone."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .bars import TF_1D, TF_1H
from .clock import ET
from .econ import news_split
from .strategy import GATE_REASONS
from .twin import edge

SLEEVES = ("S0", "S1", "S2", "S3")
BRIEF_SYMBOLS = ("SPY", "QQQ", "BTC/USD", "GLD", "USO")


def money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+,.2f}"


def _prev_close(store, sym: str, today: date) -> Optional[tuple[float, str]]:
    """Last price of the previous session: the last stored 1-min close, else the last daily bar."""
    dates = store.bar_dates(1, symbols=[sym], before=today)
    if dates:
        bars = store.bars_for_day(dates[0], symbol=sym).get(sym)
        if bars:
            return bars[-1].c, dates[0]
    d = store.bars_tf(sym, TF_1D, None, datetime(today.year, today.month, today.day, tzinfo=ET), limit=1)
    return (d[-1].c, d[-1].ts.date().isoformat()) if d else None


def morning_brief(store, broker, clock, now: datetime) -> str:
    today = now.date()
    lines = [f"Trading Chambers - morning {now.strftime('%a %b %d')}"]
    try:
        latest = broker.latest_prices(list(BRIEF_SYMBOLS)) if broker is not None else {}
    except Exception:
        latest = {}
    prev_session = None
    if clock is not None:
        try:
            ss = clock.sessions_between(today - timedelta(days=7), today - timedelta(days=1))
            prev_session = ss[max(ss)] if ss else None
        except Exception:
            prev_session = None
    lines.append("Overnight:")
    for sym in BRIEF_SYMBOLS:
        now_px = latest.get(sym)
        if sym == "BTC/USD":
            cut = prev_session.close if prev_session else datetime(today.year, today.month, today.day, tzinfo=ET) \
                - timedelta(hours=8)
            b = store.bars_tf(sym, TF_1H, None, cut, limit=1)
            ref = (b[-1].c, "prev close") if b else None
        else:
            ref = _prev_close(store, sym, today)
        if ref and now_px:
            lines.append(f"  {sym.split('/')[0]} {now_px:,.2f} ({(now_px / ref[0] - 1) * 100:+.2f}%)")
        elif now_px:
            lines.append(f"  {sym.split('/')[0]} {now_px:,.2f} (no prior close stored)")
        else:
            lines.append(f"  {sym.split('/')[0]} n/a")
    ev = store.econ_events_on(today)
    lines.append("Today: " + ("; ".join(f"{e['event']} {e['time'] or ''}".strip() for e in ev) + " - NEWS DAY"
                              if ev else "no scheduled high-impact events"))
    for sid in SLEEVES:
        s = store.get_sleeve(sid)
        if s is None or not s["active"]:
            continue
        open_ = store.open_trades(sid)
        pr = store.read_params(sid)
        pos = ", ".join(f"{t.symbol} {t.side} {t.qty:g}@{t.entry_price:,.2f}" for t in open_) or "flat"
        lines.append(f"{sid} {s['name']}: {pos}")
        if pr:
            lines.append("  params: " + short_params(sid, pr["params"]))
    since = _last_evening_ts(store) or (now - timedelta(hours=18))
    al = [a for a in store.alerts_since(since, "alert") if a["status"] != "suppressed"]
    lines.append(f"Alerts since last evening: {len(al)}" + ("" if not al else ""))
    for a in al[-5:]:
        lines.append(f"  {a['ts'][11:16]} {a['message'][:140]}")
    return "\n".join(lines)


def short_params(sid: str, p: dict) -> str:
    keys = {"S0": ("entry_dev_pct", "vol_mult", "max_hold_bars", "stop_pct"),
            "S1": ("entry_z", "stop_atr", "max_hold_bars"),
            "S2": ("lookback", "vol_mult", "exit_lookback", "stop_atr"),
            "S3": ("fast", "slow", "stop_atr")}[sid]
    out = " ".join(f"{k}={p.get(k)}" for k in keys)
    if p.get("skip_news_days"):
        out += " skip_news_days"
    return out


def _last_evening_ts(store) -> Optional[datetime]:
    ev = store.recent_alerts(1, kind="evening")
    return datetime.fromisoformat(ev[0]["ts"]) if ev else None


def evening_report(store, d: date, account_equity: Optional[float] = None) -> str:
    ds = d.isoformat()
    lines = [f"Trading Chambers - evening {d.strftime('%a %b %d')}"]
    total = 0.0
    start20 = (d - timedelta(days=40)).isoformat()
    for sid in SLEEVES:
        s = store.get_sleeve(sid)
        if s is None or not s["active"]:
            continue
        closed = store.closed_trades_for_day(d, sid)
        e = edge(store, sid, ds)
        total += e["sleeve_net"]
        lines.append(f"{sid} {s['name']}: {len(closed)} trades, net {money(e['sleeve_net'])}, twin "
                     f"{money(e['twin_net'])}, edge {money(e['edge'])} (20d {money(e['edge_trailing'])})")
        sw = [r for r in store.params_history(3, "sweep", sid) if r["date"] == ds]
        if sw:
            ss = sw[0]["sweep_summary"] or {}
            lines.append(f"  params: {'CHANGED ' if ss.get('changed') else ''}{(ss.get('reason') or '')[:120]}")
        ns = news_split(store, sid, start20, ds)["sleeve"]
        if ns["news"]["trades"]:
            lines.append(f"  news days {money(ns['news']['net'])} ({ns['news']['trades']} tr) vs other "
                         f"{money(ns['non_news']['net'])} ({ns['non_news']['trades']} tr)")
    lines.append(f"Portfolio: net today {money(total)}" + (f", equity {account_equity:,.2f}" if account_equity else ""))
    rd = store.risk_day(d)
    if rd and rd.get("open_equity") and account_equity:
        lines.append(f"  day P&L vs open {money(account_equity - rd['open_equity'])}")
    rec = [r for r in store.recon_for_day(d) if r["kind"] == "equity_session"]
    if rec:
        r = rec[-1]
        lines.append(f"Recon: {r['status']}" + (f" (diff {money(r['diff'])}, limit {r['threshold']:,.2f})"
                                                if r.get("diff") is not None else ""))
    else:
        lines.append("Recon: not run yet")
    cb = store.reason_counts(d, GATE_REASONS)
    halt = rd and rd.get("halted")
    parts = []
    if halt:
        parts.append(f"DAILY LOSS HALT at {rd['halted_ts'][11:16]}")
    parts += [f"{k} x{v}" for k, v in sorted(cb.items()) if k != "daily_loss_halt"]
    lines.append("Circuit breakers: " + (", ".join(parts) if parts else "none tripped"))
    sug = store.lab_suggestions(["pending"], limit=5)
    if sug:
        lines.append(f"Night lab: {len(sug)} suggestion(s) awaiting your approval")
        for x in sug[:3]:
            lines.append(f"  {x['candidate']}: graded net {money(x['grade_net'])} vs twin {money(x['twin_grade_net'])} "
                         f"over {x['n_sessions']} sessions")
    else:
        lines.append("Night lab: no suggestions")
    p = store.p100_ledger_for(d)
    if p:
        led = store.p100_ledger(20)
        first = led[-1]["start_equity"] if led else p["start_equity"]
        sk = (p.get("detail") or {}).get("skipped") or {}
        cash = sk.get("no_settled_cash", 0) + sk.get("below_min_order", 0)
        lines.append(f"$100 profile: equity {p['end_equity']:.2f} (day {money(p['net_pnl'])}, {p['trades']} trades, "
                     f"{p['skipped']} skipped, {cash} for unsettled cash; {len(led)}d {money(p['end_equity'] - first)}); "
                     f"shadow shorts {p['shadow_trades']} ({money(p['shadow_net'])}, learning only)")
    else:
        lines.append("$100 profile: not run")
    n_err = store.errors_count(d=d)
    unh = store.errors_count(d=d, where_like="unhandled%")
    lines.append(f"Errors today: {n_err}" + (f" ({unh} UNHANDLED)" if unh else ""))
    return "\n".join(lines)
