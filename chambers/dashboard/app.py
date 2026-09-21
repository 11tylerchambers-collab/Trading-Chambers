"""Dashboard + control API (spec §15).

Reads the database; writes only `controls` and `params`. One shared password,
bearer tokens kept in process memory. Runs in a daemon thread inside the
engine process (see main.cmd_run) with its own Store connection.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from ..clock import ET, MarketClock, now_et
from ..data import SymbolState
from ..store import Store
from ..strategy import PARAM_KEYS, Params
from ..sweep import GRID

log = logging.getLogger("chambers.dashboard")
STATIC = Path(__file__).resolve().parent / "static"
ACCOUNT_CACHE_S = 30.0


def create_app(db_path, password: str, broker=None, clock: Optional[MarketClock] = None,
               universe: Optional[list[str]] = None) -> FastAPI:
    app = FastAPI(title="Trading Chambers — Phase 0", docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(db_path)
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

    # ---- state -----------------------------------------------------------------
    def account_info() -> Optional[dict]:
        if broker is None:
            return None
        with lock:
            if time.monotonic() - account_cache["ts"] < ACCOUNT_CACHE_S and account_cache["data"] is not None:
                return account_cache["data"]
        try:
            a = broker.account()
            data = {"portfolio_value": a.get("portfolio_value"), "buying_power": a.get("buying_power")}
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

    def open_positions(now: datetime, params: Params) -> list[dict]:
        out = []
        bars_by_day: dict[str, dict] = {}
        for t in store.open_trades():
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

    def sweep_history() -> list[dict]:
        rows = store.params_history(8, source="sweep")  # newest first; one extra for the delta of the oldest
        out = []
        for i, r in enumerate(rows[:7]):
            prev = rows[i + 1]["params"] if i + 1 < len(rows) else None
            delta = {k: [prev.get(k), r["params"].get(k)] for k in GRID
                     if prev is not None and prev.get(k) != r["params"].get(k)} if prev else {}
            ss = r.get("sweep_summary") or {}
            chosen_stats = next((x for x in ss.get("results", [])
                                 if all(x["params"].get(k) == r["params"].get(k) for k in GRID)), None)
            out.append({"date": r["date"], "params": {k: r["params"].get(k) for k in GRID}, "delta": delta,
                        "changed": ss.get("changed"), "reason": ss.get("reason"),
                        "today_closed_trades": ss.get("today_closed_trades"),
                        "replay_trades": chosen_stats["trades"] if chosen_stats else None,
                        "replay_net_pnl": chosen_stats["net_pnl"] if chosen_stats else None,
                        "days": ss.get("days"), "duration_s": ss.get("duration_s")})
        return out

    @app.get("/api/state")
    async def state(_tok: str = Depends(require_token)):
        now = now_et()
        prow = store.read_params()
        params = Params.from_dict(prow["params"]) if prow else Params()
        return {
            "now": now.isoformat(),
            "heartbeat": store.read_heartbeat(),
            "market": market_info(now),
            "account": account_info(),
            "controls": store.read_controls(),
            "open_positions": open_positions(now, params),
            "recent_trades": [t.to_dict() for t in store.recent_closed_trades(25)],
            "params": {"params": params.to_dict(), "source": prow["source"] if prow else "default",
                       "updated_at": prow["updated_at"] if prow else None},
            "sweep_history": sweep_history(),
            "errors": store.recent_errors(10),
        }

    # ---- controls ---------------------------------------------------------------
    @app.post("/api/params")
    async def set_params(body: dict, _tok: str = Depends(require_token)):
        incoming = body.get("params", body)
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail="params must be an object")
        unknown = set(incoming) - set(PARAM_KEYS)
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown params: {sorted(unknown)}")
        prow = store.read_params()
        base = prow["params"] if prow else Params().to_dict()
        try:
            p = Params.from_dict({**base, **incoming})
            p.validate()
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        now = now_et()
        store.write_params(p.to_dict(), "manual", now)
        store.write_params_history(now.date(), p.to_dict(), "manual", None)
        return {"ok": True, "params": p.to_dict()}

    @app.post("/api/pause")
    async def pause(body: dict, _tok: str = Depends(require_token)):
        paused = bool(body.get("paused", True))
        store.set_paused(paused, now_et())
        return {"ok": True, "paused": paused}

    @app.post("/api/flatten")
    async def flatten(_tok: str = Depends(require_token)):
        store.request_flatten(now_et())
        return {"ok": True, "flatten_requested": True}

    return app


def serve(db_path, password: str, broker=None, clock: Optional[MarketClock] = None,
          universe: Optional[list[str]] = None, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn
    app = create_app(db_path, password, broker, clock, universe)
    log.info("dashboard on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
