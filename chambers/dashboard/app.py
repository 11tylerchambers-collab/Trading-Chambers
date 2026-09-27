"""Dashboard + control API (Phase 0 spec §15, PHASE1A §11).

Reads the database; writes only `controls`, `params` (manual edits) and lab approvals. One shared
password, bearer tokens kept in process memory. Runs in a daemon thread inside the engine process
(see main.cmd_run) with its own Store connection.

`GET /api/state?sleeve=All|S0|S1|S2|S3|P100` — without `sleeve` the per-sleeve sections are S0's, so
the Phase 0 page contract is unchanged. Every response also carries the sleeves overview, the
portfolio card, the night lab panel and the last 20 alerts.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from ..bars import TF_1H
from ..clock import ET, MarketClock, now_et
from ..data import SymbolState
from ..store import Store
from ..strategies import SPECS
from ..strategy import PARAM_KEYS, Params
from ..sweep import GRID
from ..twin import edge

log = logging.getLogger("chambers.dashboard")
STATIC = Path(__file__).resolve().parent / "static"
ACCOUNT_CACHE_S = 30.0
SLEEVE_IDS = ("S0", "S1", "S2", "S3")
VIEWS = ("All",) + SLEEVE_IDS + ("P100",)


def param_fields(sleeve_id: str) -> list[dict]:
    if sleeve_id == "S0":
        types = {"allow_short": bool, "skip_news_days": bool, "max_hold_bars": int, "max_open_positions": int,
                 "min_bars_before_entry": int, "reentry_cooldown_bars": int}
        return [{"key": k, "type": "bool" if types.get(k) is bool else "number",
                 "step": 1 if types.get(k) is int else 0.01} for k in PARAM_KEYS]
    spec = SPECS[sleeve_id]
    return [{"key": k, "type": "bool" if t is bool else "number", "step": 1 if t is int else 0.01}
            for k, t in spec.types.items()]


def create_app(db_path, password: str, broker=None, clock: Optional[MarketClock] = None,
               universe: Optional[list[str]] = None, max_same_side: int = 15,
               daily_loss_pct: float = 0.02, now_fn=None) -> FastAPI:
    app = FastAPI(title="Trading Chambers", docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(db_path)
    clock_now = now_fn or now_et
    tokens: set[str] = set()
    account_cache = {"ts": 0.0, "data": None}
    lock = threading.Lock()

    # ---- auth ---------------------------------------------------------------
    def require_token(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not tok or tok not in tokens:
            raise HTTPException(status_code=401, detail="unauthorized")
        return tok

    @app.post("/api/login")
    async def login(body: dict):
        given = str(body.get("password", ""))
        if not password:
            raise HTTPException(status_code=503, detail="DASH_PASSWORD is not set on the host")
        if not hmac.compare_digest(given.encode(), password.encode()):
            raise HTTPException(status_code=401, detail="wrong password")
        tok = secrets.token_urlsafe(32)
        tokens.add(tok)
        return {"token": tok}

    # ---- page ----------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    # ---- shared pieces --------------------------------------------------------
    def account_info() -> Optional[dict]:
        if broker is None:
            return None
        with lock:
            if time.monotonic() - account_cache["ts"] < ACCOUNT_CACHE_S and account_cache["data"] is not None:
                return account_cache["data"]
        try:
            a = broker.account()
            data = {"portfolio_value": a.get("portfolio_value"), "buying_power": a.get("buying_power")}
            account_cache["equity"] = a.get("equity")
        except Exception as e:  # BrokerError already logged by the wrapper
            log.warning("account() failed: %s", e)
            data = account_cache["data"]
        with lock:
            account_cache["ts"] = time.monotonic()
            account_cache["data"] = data
        return data

    def market_info(now: datetime) -> dict:
        out = {"is_open": None, "session_open": None, "session_close": None, "flatten_at": None,
               "entries_allowed": None, "next_open": None}
        if clock is None:
            return out
        try:
            s = clock.today_session()
            out["is_open"] = clock.is_market_open()
            out["entries_allowed"] = clock.entries_allowed()
            if s:
                out.update(session_open=s.open.isoformat(), session_close=s.close.isoformat(),
                           flatten_at=s.flatten_at.isoformat())
            nxt = clock.next_session_open()
            out["next_open"] = nxt.isoformat() if nxt else None
        except Exception as e:
            log.warning("market_info failed: %s", e)
        return out

    # ---- S0 (Phase 0) sections --------------------------------------------------
    def s0_positions(params: Params) -> list[dict]:
        out = []
        bars_by_day: dict[str, dict] = {}
        for t in store.open_trades("S0"):
            day = t.entry_ts[:10]  # the session the position belongs to
            if day not in bars_by_day:
                bars_by_day[day] = store.bars_for_day(day)
            st = SymbolState(t.symbol)
            st.update(bars_by_day[day].get(t.symbol, []))
            cur, vwap = st.last_close, st.vwap
            row = t.to_dict()
            row.update(current_price=cur, vwap=vwap, unrealized_pl=None, to_vwap_pct=None, to_stop_pct=None,
                       bars_left=max(0, params.max_hold_bars - t.bars_held))
            if cur:
                sign = 1 if t.side == "long" else -1
                row["unrealized_pl"] = (cur - t.entry_price) * t.qty * sign
                adverse = -((cur - t.entry_price) / t.entry_price * 100 * sign)
                row["to_stop_pct"] = params.stop_pct - adverse
                if vwap:
                    row["to_vwap_pct"] = (vwap - cur) / cur * 100 * sign
            out.append(row)
        return out

    def s0_sweep_history() -> list[dict]:
        rows = store.params_history(8, source="sweep")  # newest first; one extra for the delta of the oldest
        out = []
        for i, r in enumerate(rows[:7]):
            ss = r.get("sweep_summary") or {}
            # baseline = the live params the sweep started from (a manual edit may sit between sweep rows)
            prev = ss.get("current") or (rows[i + 1]["params"] if i + 1 < len(rows) else None)
            delta = {k: [prev.get(k), r["params"].get(k)] for k in GRID
                     if prev.get(k) != r["params"].get(k)} if prev else {}
            chosen_stats = next((x for x in ss.get("results", [])
                                 if all(x["params"].get(k) == r["params"].get(k) for k in GRID)), None)
            out.append({"date": r["date"], "params": {k: r["params"].get(k) for k in GRID}, "delta": delta,
                        "changed": ss.get("changed"), "reason": ss.get("reason"),
                        "today_closed_trades": ss.get("today_closed_trades"),
                        "replay_trades": chosen_stats["trades"] if chosen_stats else None,
                        "replay_net_pnl": chosen_stats["net_pnl"] if chosen_stats else None,
                        "days": ss.get("days"), "duration_s": ss.get("duration_s")})
        return out

    # ---- S1–S3 sections -----------------------------------------------------------
    def last_price(symbol: str) -> Optional[float]:
        if "/" in symbol:
            b = store.bars_tf(symbol, TF_1H, limit=1)
            return b[-1].c if b else None
        d = store.bar_dates(1, symbols=[symbol], before=clock_now().date() + timedelta(days=1))
        if not d:
            return None
        bs = store.bars_for_day(d[0], symbol=symbol).get(symbol)
        return bs[-1].c if bs else None

    def bar_positions(sleeve_id: str) -> list[dict]:
        out = []
        for t in store.open_trades(sleeve_id):
            row = t.to_dict()
            cur = last_price(t.symbol)
            stop = (t.hypothesis or {}).get("stop_price")
            sign = 1 if t.side == "long" else -1
            row.update(current_price=cur, stop_price=stop, unrealized_pl=None, to_stop_pct=None, to_vwap_pct=None,
                       bars_left=None)
            if cur:
                row["unrealized_pl"] = (cur - t.entry_price) * t.qty * sign
                if stop:
                    row["to_stop_pct"] = (cur - stop) / cur * 100 * sign
            out.append(row)
        return out

    def bar_sweep_history(sleeve_id: str, profile: str = "LIVE") -> list[dict]:
        spec = SPECS.get(sleeve_id)
        keys = list(spec.grid) if spec else list(GRID)
        out = []
        for r in store.params_history(7, "sweep", sleeve_id, profile):
            ss = r.get("sweep_summary") or {}
            prev = ss.get("current") or {}
            delta = {k: [prev.get(k), r["params"].get(k)] for k in keys if prev and prev.get(k) != r["params"].get(k)}
            out.append({"date": r["date"], "params": {k: r["params"].get(k) for k in keys}, "delta": delta,
                        "changed": ss.get("changed"), "reason": ss.get("reason"),
                        "evidence_trades": ss.get("evidence_trades"), "days": ss.get("days"),
                        "today_closed_trades": ss.get("today_closed_trades"),
                        "replay_trades": None, "replay_net_pnl": None, "duration_s": ss.get("duration_s")})
        return out

    def sleeve_params(sleeve_id: str, profile: str = "LIVE") -> dict:
        row = store.read_params(sleeve_id, profile)
        if sleeve_id == "S0":
            p = Params.from_dict(row["params"]).to_dict() if row else Params().to_dict()
        else:
            p = SPECS[sleeve_id].params(row["params"] if row else {})
        return {"params": p, "source": row["source"] if row else "default", "updated_at": row["updated_at"] if row else None,
                "fields": param_fields(sleeve_id), "sleeve_id": sleeve_id, "profile": profile}

    # ---- overview, portfolio, lab, alerts -------------------------------------------
    def sleeves_overview(today: str) -> list[dict]:
        hbs = store.read_heartbeats()
        out = []
        for s in store.sleeves():
            sid = s["id"]
            hb = hbs.get(sid) or {}
            e = edge(store, sid, today)
            out.append({"id": sid, "name": s["name"], "active": s["active"], "capital": s["capital"],
                        "equity": store.sleeve_equity(sid), "symbols": s["symbols"], "timeframe": s["timeframe"],
                        "state": hb.get("state"), "last_cycle_ts": hb.get("last_cycle_ts"),
                        "cycles_today": hb.get("cycles_today"), "opened_today": hb.get("opened_today"),
                        "closed_today": hb.get("closed_today"), "open_positions": len(store.open_trades(sid)),
                        "net_pnl_today": e["sleeve_net"], "twin_net_today": e["twin_net"], "edge_today": e["edge"],
                        "edge_trailing": e["edge_trailing"], "trailing_sessions": e["sessions"]})
        return out

    def portfolio(now: datetime) -> dict:
        acct = account_info() or {}
        eq = account_cache.get("equity") or acct.get("portfolio_value")
        opens = store.open_trades(None)
        gross = sum(abs(t.qty * t.entry_price) for t in opens)
        rd = store.risk_day(now.date()) or {}
        oe = rd.get("open_equity")
        rec = store.recent_recon(1)
        return {"equity": eq, "open_equity": oe, "daily_pnl": (eq - oe) if (eq and oe) else None,
                "loss_limit": (-daily_loss_pct * oe) if oe else None, "halted": bool(rd.get("halted")),
                "halted_ts": rd.get("halted_ts"), "exposure": gross,
                "exposure_pct": (gross / eq * 100) if eq else None,
                "long": sum(1 for t in opens if t.side == "long"), "short": sum(1 for t in opens if t.side == "short"),
                "max_same_side": max_same_side, "last_recon": rec[0] if rec else None}

    def lab_panel() -> dict:
        runs = store.lab_runs(1)
        return {"last_run": runs[0] if runs else None, "results": store.lab_results(),
                "suggestions": store.lab_suggestions(limit=10)}

    def p100_view() -> dict:
        led = store.p100_ledger(20)
        last = led[0] if led else None
        return {"ledger": led, "trades": store.p100_trades_for(last["date"]) if last else [],
                "params": {sid: sleeve_params(sid, "P100") for sid in ("S0", "S1")},
                "sweep_history": {"S0": bar_sweep_history("S0", "P100"), "S1": bar_sweep_history("S1", "P100")}}

    @app.get("/api/state")
    async def state(sleeve: str = "S0", _tok: str = Depends(require_token)):
        if sleeve not in VIEWS:
            raise HTTPException(status_code=400, detail=f"sleeve must be one of {VIEWS}")
        now = clock_now()
        today = now.date().isoformat()
        out = {
            "now": now.isoformat(),
            "sleeve": sleeve,
            "market": market_info(now),
            "account": account_info(),
            "controls": store.read_controls(),
            "errors": store.recent_errors(10),
            "sleeves": sleeves_overview(today),
            "portfolio": portfolio(now),
            "lab": lab_panel(),
            "alerts": store.recent_alerts(20),
        }
        if sleeve == "S0":
            prow = store.read_params("S0")
            params = Params.from_dict(prow["params"]) if prow else Params()
            out.update(heartbeat=store.read_heartbeat("S0"), open_positions=s0_positions(params),
                       recent_trades=[t.to_dict() for t in store.recent_closed_trades(25, "S0")],
                       params={"params": params.to_dict(), "source": prow["source"] if prow else "default",
                               "updated_at": prow["updated_at"] if prow else None, "fields": param_fields("S0"),
                               "sleeve_id": "S0", "profile": "LIVE"},
                       sweep_history=s0_sweep_history(), edge=edge(store, "S0", today))
        elif sleeve in SLEEVE_IDS:
            out.update(heartbeat=store.read_heartbeat(sleeve), open_positions=bar_positions(sleeve),
                       recent_trades=[t.to_dict() for t in store.recent_closed_trades(25, sleeve)],
                       params=sleeve_params(sleeve), sweep_history=bar_sweep_history(sleeve),
                       edge=edge(store, sleeve, today))
        elif sleeve == "All":
            prow = store.read_params("S0")
            s0p = Params.from_dict(prow["params"]) if prow else Params()
            pos = s0_positions(s0p) + [p for sid in ("S1", "S2", "S3") for p in bar_positions(sid)]
            out.update(heartbeat=store.read_heartbeat("ALL"), open_positions=pos,
                       recent_trades=[t.to_dict() for t in store.recent_closed_trades(25, None)],
                       params=None, sweep_history=[], edge=None)
        else:  # P100
            out.update(heartbeat=None, open_positions=[], recent_trades=[], params=None, sweep_history=[], edge=None,
                       p100=p100_view())
        return out

    # ---- controls ---------------------------------------------------------------
    @app.post("/api/params")
    async def set_params(body: dict, _tok: str = Depends(require_token)):
        sleeve_id = body.get("sleeve_id", "S0") if isinstance(body, dict) else "S0"
        if sleeve_id not in SLEEVE_IDS:
            raise HTTPException(status_code=400, detail=f"sleeve_id must be one of {SLEEVE_IDS}")
        incoming = body.get("params", body)
        if isinstance(incoming, dict) and incoming is body:
            incoming = {k: v for k, v in body.items() if k != "sleeve_id"}
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail="params must be an object")
        allowed = set(PARAM_KEYS) if sleeve_id == "S0" else set(SPECS[sleeve_id].types)
        unknown = set(incoming) - allowed
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown params: {sorted(unknown)}")
        prow = store.read_params(sleeve_id)
        try:
            if sleeve_id == "S0":
                base = prow["params"] if prow else Params().to_dict()
                p = Params.from_dict({**base, **incoming})
                p.validate()
                newp = p.to_dict()
            else:
                spec = SPECS[sleeve_id]
                newp = spec.params({**(prow["params"] if prow else {}), **incoming})
                spec.validate(newp)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        now = now_et()
        store.write_params(newp, "manual", now, sleeve_id)
        store.write_params_history(now.date(), newp, "manual", None, sleeve_id)
        return {"ok": True, "sleeve_id": sleeve_id, "params": newp}

    @app.post("/api/pause")
    async def pause(body: dict, _tok: str = Depends(require_token)):
        paused = bool(body.get("paused", True))
        store.set_paused(paused, now_et())
        return {"ok": True, "paused": paused}

    @app.post("/api/flatten")
    async def flatten(_tok: str = Depends(require_token)):
        store.request_flatten(now_et())
        return {"ok": True, "flatten_requested": True}

    @app.post("/api/lab/decide")
    async def lab_decide(body: dict, _tok: str = Depends(require_token)):
        """Records the user's approval or rejection of a night-lab suggestion. Nothing is traded (§7)."""
        try:
            sid, status = int(body.get("id")), str(body.get("status"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="id and status are required")
        if status not in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail="status must be approved or rejected")
        if not store.decide_lab_suggestion(sid, status, now_et(), body.get("note")):
            raise HTTPException(status_code=409, detail="no pending suggestion with that id")
        return {"ok": True, "id": sid, "status": status}

    return app


def serve(db_path, password: str, broker=None, clock: Optional[MarketClock] = None,
          universe: Optional[list[str]] = None, host: str = "0.0.0.0", port: int = 8080, **kw) -> None:
    import uvicorn
    app = create_app(db_path, password, broker, clock, universe, **kw)
    log.info("dashboard on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
