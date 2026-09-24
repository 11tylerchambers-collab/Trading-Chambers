"""SQLite storage. All SQL lives in this module.

One file (data/chambers.db), WAL mode. The engine process is the single
writer; the dashboard reads, and writes only `controls` and `params`.
All timestamps are stored as ISO-8601 strings with offset, in Eastern Time.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  state TEXT NOT NULL,
  symbols_evaluated INTEGER NOT NULL DEFAULT 0,
  signals_fired INTEGER NOT NULL DEFAULT 0,
  orders_placed INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);

CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id INTEGER,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  close REAL,
  vwap REAL,
  dev_pct REAL,
  vol_ratio REAL,
  side TEXT,
  fired INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,
  params_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  entry_ts TEXT NOT NULL,
  entry_price REAL NOT NULL,
  entry_order_id TEXT,
  entry_bid REAL,
  entry_ask REAL,
  hypothesis_json TEXT,
  exit_ts TEXT,
  exit_price REAL,
  exit_order_id TEXT,
  exit_bid REAL,
  exit_ask REAL,
  exit_reason TEXT,
  bars_held INTEGER NOT NULL DEFAULT 0,
  mae_pct REAL NOT NULL DEFAULT 0,
  mfe_pct REAL NOT NULL DEFAULT 0,
  gross_pnl REAL,
  est_cost REAL,
  net_pnl REAL,
  params_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(exit_ts) WHERE exit_ts IS NULL;

CREATE TABLE IF NOT EXISTS bars(
  symbol TEXT NOT NULL,
  ts TEXT NOT NULL,
  o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
  v REAL NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(ts);

CREATE TABLE IF NOT EXISTS heartbeat(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,
  last_cycle_ts TEXT,
  cycles_today INTEGER NOT NULL DEFAULT 0,
  signals_today INTEGER NOT NULL DEFAULT 0,
  fired_today INTEGER NOT NULL DEFAULT 0,
  opened_today INTEGER NOT NULL DEFAULT 0,
  closed_today INTEGER NOT NULL DEFAULT 0,
  open_positions INTEGER NOT NULL DEFAULT 0,
  net_pnl_today REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  last_error_ts TEXT,
  pid INTEGER,
  started_at TEXT
);

CREATE TABLE IF NOT EXISTS params(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  params_json TEXT NOT NULL,
  source TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS params_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  params_json TEXT NOT NULL,
  source TEXT NOT NULL,
  sweep_summary_json TEXT
);

CREATE TABLE IF NOT EXISTS controls(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  paused INTEGER NOT NULL DEFAULT 0,
  flatten_requested INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  where_ TEXT NOT NULL,
  message TEXT NOT NULL,
  traceback TEXT
);
CREATE INDEX IF NOT EXISTS idx_errors_ts ON errors(ts);
"""


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def iso(dt: datetime) -> str:
    """Serialize an aware datetime as ISO-8601 with offset, in Eastern Time."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed in store")
    return dt.astimezone(ET).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(ET)


def day_of(s: str) -> str:
    """YYYY-MM-DD prefix of a stored ET timestamp."""
    return s[:10]


def _date_str(d: date | str) -> str:
    return d if isinstance(d, str) else d.isoformat()


# --------------------------------------------------------------------------
# row types
# --------------------------------------------------------------------------

@dataclass
class Bar:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float

    def to_row(self) -> dict:
        return {"ts": iso(self.ts), "o": self.o, "h": self.h, "l": self.l, "c": self.c, "v": self.v}


@dataclass
class Trade:
    id: int
    symbol: str
    side: str
    qty: int
    entry_ts: str
    entry_price: float
    entry_order_id: Optional[str]
    entry_bid: Optional[float]
    entry_ask: Optional[float]
    hypothesis_json: Optional[str]
    exit_ts: Optional[str]
    exit_price: Optional[float]
    exit_order_id: Optional[str]
    exit_bid: Optional[float]
    exit_ask: Optional[float]
    exit_reason: Optional[str]
    bars_held: int
    mae_pct: float
    mfe_pct: float
    gross_pnl: Optional[float]
    est_cost: Optional[float]
    net_pnl: Optional[float]
    params_json: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["hypothesis"] = json.loads(self.hypothesis_json) if self.hypothesis_json else None
        return d


class Store:
    """Typed access to the SQLite database. Thread-safe via a lock."""

    def __init__(self, path: str | os.PathLike = "data/chambers.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- low level -------------------------------------------------------
    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            self._conn.commit()
            return cur

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchone()

    # ---- cycles ------------------------------------------------------------
    def write_cycle(self, ts: datetime, state: str, symbols_evaluated: int, signals_fired: int,
                    orders_placed: int, errors: int, duration_ms: int) -> int:
        cur = self._exec(
            "INSERT INTO cycles(ts,state,symbols_evaluated,signals_fired,orders_placed,errors,duration_ms)"
            " VALUES(?,?,?,?,?,?,?)",
            (iso(ts), state, symbols_evaluated, signals_fired, orders_placed, errors, duration_ms))
        return int(cur.lastrowid)

    def cycles_for_day(self, d: date | str) -> list[dict]:
        rows = self._query("SELECT * FROM cycles WHERE substr(ts,1,10)=? ORDER BY ts", (_date_str(d),))
        return [dict(r) for r in rows]

    def cycle_dates(self, limit: int = 5) -> list[str]:
        """The last `limit` dates with at least one `running` cycle (a `--once` or after-hours start isn't a session)."""
        rows = self._query(
            "SELECT DISTINCT substr(ts,1,10) AS d FROM cycles WHERE state='running' ORDER BY d DESC LIMIT ?", (limit,))
        return sorted(r["d"] for r in rows)

    # ---- signals -----------------------------------------------------------
    def write_signals(self, cycle_id: Optional[int], rows: list[dict]) -> None:
        """rows: dicts with ts(datetime), symbol, close, vwap, dev_pct, vol_ratio, side, fired, reason, params(dict)."""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO signals(cycle_id,ts,symbol,close,vwap,dev_pct,vol_ratio,side,fired,reason,params_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [(cycle_id, iso(r["ts"]), r["symbol"], r.get("close"), r.get("vwap"), r.get("dev_pct"),
                  r.get("vol_ratio"), r.get("side"), 1 if r.get("fired") else 0, r["reason"],
                  json.dumps(r["params"]) if r.get("params") is not None else None) for r in rows])
            self._conn.commit()

    def write_signal(self, cycle_id: Optional[int], **row) -> None:
        self.write_signals(cycle_id, [row])

    def signals_for_day(self, d: date | str) -> list[dict]:
        rows = self._query("SELECT * FROM signals WHERE substr(ts,1,10)=? ORDER BY id", (_date_str(d),))
        return [dict(r) for r in rows]

    def signals_count_for_day(self, d: date | str, fired_only: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM signals WHERE substr(ts,1,10)=?"
        if fired_only:
            sql += " AND fired=1"
        return int(self._one(sql, (_date_str(d),))[0])

    # ---- trades ------------------------------------------------------------
    def open_trade(self, symbol: str, side: str, qty: int, entry_ts: datetime, entry_price: float,
                   entry_order_id: Optional[str], entry_bid: Optional[float], entry_ask: Optional[float],
                   hypothesis: dict, params: dict) -> int:
        cur = self._exec(
            "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
            "hypothesis_json,params_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (symbol, side, qty, iso(entry_ts), entry_price, entry_order_id, entry_bid, entry_ask,
             json.dumps(hypothesis), json.dumps(params)))
        return int(cur.lastrowid)

    def split_trade(self, trade_id: int, keep_qty: int) -> int:
        """Shrink open trade `trade_id` to `keep_qty` and insert an identical open trade for the rest
        (a partially filled exit). Returns the new trade's id."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
                "hypothesis_json,bars_held,mae_pct,mfe_pct,params_json) "
                "SELECT symbol,side,qty-?,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
                "hypothesis_json,bars_held,mae_pct,mfe_pct,params_json FROM trades WHERE id=?", (keep_qty, trade_id))
            new_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._conn.execute("UPDATE trades SET qty=? WHERE id=?", (keep_qty, trade_id))
            self._conn.commit()
        return int(new_id)

    def update_trade_progress(self, trade_id: int, bars_held: int, mae_pct: float, mfe_pct: float) -> None:
        self._exec("UPDATE trades SET bars_held=?, mae_pct=?, mfe_pct=? WHERE id=?",
                   (bars_held, mae_pct, mfe_pct, trade_id))

    def close_trade(self, trade_id: int, exit_ts: datetime, exit_price: float, exit_order_id: Optional[str],
                    exit_bid: Optional[float], exit_ask: Optional[float], exit_reason: str,
                    bars_held: int, mae_pct: float, mfe_pct: float,
                    gross_pnl: float, est_cost: float, net_pnl: float) -> None:
        self._exec(
            "UPDATE trades SET exit_ts=?, exit_price=?, exit_order_id=?, exit_bid=?, exit_ask=?, exit_reason=?,"
            " bars_held=?, mae_pct=?, mfe_pct=?, gross_pnl=?, est_cost=?, net_pnl=? WHERE id=?",
            (iso(exit_ts), exit_price, exit_order_id, exit_bid, exit_ask, exit_reason,
             bars_held, mae_pct, mfe_pct, gross_pnl, est_cost, net_pnl, trade_id))

    def update_trade_economics(self, trade_id: int, gross_pnl: float, est_cost: float, net_pnl: float) -> None:
        self._exec("UPDATE trades SET gross_pnl=?, est_cost=?, net_pnl=? WHERE id=?",
                   (gross_pnl, est_cost, net_pnl, trade_id))

    def all_closed_trades(self) -> list[Trade]:
        return [self._trade(r) for r in self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL ORDER BY id")]

    def _trade(self, r: sqlite3.Row) -> Trade:
        return Trade(**{k: r[k] for k in r.keys()})

    def get_trade(self, trade_id: int) -> Optional[Trade]:
        r = self._one("SELECT * FROM trades WHERE id=?", (trade_id,))
        return self._trade(r) if r else None

    def open_trades(self) -> list[Trade]:
        return [self._trade(r) for r in self._query("SELECT * FROM trades WHERE exit_ts IS NULL ORDER BY id")]

    def closed_trades_for_day(self, d: date | str) -> list[Trade]:
        rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL AND substr(exit_ts,1,10)=? ORDER BY exit_ts",
                           (_date_str(d),))
        return [self._trade(r) for r in rows]

    def recent_closed_trades(self, limit: int = 25) -> list[Trade]:
        rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL ORDER BY exit_ts DESC, id DESC LIMIT ?",
                           (limit,))
        return [self._trade(r) for r in rows]

    def opened_count_for_day(self, d: date | str) -> int:
        return int(self._one("SELECT COUNT(*) FROM trades WHERE substr(entry_ts,1,10)=?", (_date_str(d),))[0])

    # ---- bars --------------------------------------------------------------
    def write_bars(self, symbol: str, bars: Iterable[Bar]) -> int:
        rows = [(symbol, iso(b.ts), b.o, b.h, b.l, b.c, b.v) for b in bars]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO bars(symbol,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", rows)
            self._conn.commit()
            return cur.rowcount

    def bars_for_day(self, d: date | str, symbol: Optional[str] = None) -> dict[str, list[Bar]]:
        if symbol is None:
            rows = self._query("SELECT * FROM bars WHERE substr(ts,1,10)=? ORDER BY symbol, ts", (_date_str(d),))
        else:
            rows = self._query("SELECT * FROM bars WHERE substr(ts,1,10)=? AND symbol=? ORDER BY ts",
                               (_date_str(d), symbol))
        out: dict[str, list[Bar]] = {}
        for r in rows:
            out.setdefault(r["symbol"], []).append(
                Bar(parse_ts(r["ts"]), r["o"], r["h"], r["l"], r["c"], r["v"]))
        return out

    def bar_dates(self, limit: int = 5) -> list[str]:
        rows = self._query("SELECT DISTINCT substr(ts,1,10) AS d FROM bars ORDER BY d DESC LIMIT ?", (limit,))
        return sorted(r["d"] for r in rows)

    def latest_bar_ts(self, d: date | str) -> Optional[str]:
        r = self._one("SELECT MAX(ts) FROM bars WHERE substr(ts,1,10)=?", (_date_str(d),))
        return r[0] if r and r[0] else None

    # ---- heartbeat ---------------------------------------------------------
    HEARTBEAT_FIELDS = ("state", "last_cycle_ts", "cycles_today", "signals_today", "fired_today",
                        "opened_today", "closed_today", "open_positions", "net_pnl_today",
                        "last_error", "last_error_ts", "pid", "started_at")

    def write_heartbeat(self, **fields) -> None:
        """Upsert the single heartbeat row; only the given fields change."""
        bad = set(fields) - set(self.HEARTBEAT_FIELDS)
        if bad:
            raise ValueError(f"unknown heartbeat fields: {bad}")
        for k in ("last_cycle_ts", "last_error_ts", "started_at"):
            if isinstance(fields.get(k), datetime):
                fields[k] = iso(fields[k])
        with self._lock:
            cur = self._conn.execute("SELECT id FROM heartbeat WHERE id=1").fetchone()
            if cur is None:
                self._conn.execute("INSERT INTO heartbeat(id,state) VALUES(1,?)", (fields.get("state", "idle"),))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE heartbeat SET {sets} WHERE id=1", tuple(fields.values()))
            self._conn.commit()

    def read_heartbeat(self) -> Optional[dict]:
        r = self._one("SELECT * FROM heartbeat WHERE id=1")
        return dict(r) if r else None

    # ---- params ------------------------------------------------------------
    def read_params(self) -> Optional[dict]:
        r = self._one("SELECT * FROM params WHERE id=1")
        if not r:
            return None
        return {"params": json.loads(r["params_json"]), "source": r["source"], "updated_at": r["updated_at"]}

    def write_params(self, params: dict, source: str, now: datetime) -> None:
        self._exec(
            "INSERT INTO params(id,params_json,source,updated_at) VALUES(1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET params_json=excluded.params_json, source=excluded.source,"
            " updated_at=excluded.updated_at",
            (json.dumps(params), source, iso(now)))

    def write_params_history(self, d: date | str, params: dict, source: str,
                             sweep_summary: Optional[dict]) -> int:
        cur = self._exec(
            "INSERT INTO params_history(date,params_json,source,sweep_summary_json) VALUES(?,?,?,?)",
            (_date_str(d), json.dumps(params), source,
             json.dumps(sweep_summary) if sweep_summary is not None else None))
        return int(cur.lastrowid)

    def params_history(self, limit: int = 7, source: Optional[str] = None) -> list[dict]:
        if source:
            rows = self._query("SELECT * FROM params_history WHERE source=? ORDER BY id DESC LIMIT ?", (source, limit))
        else:
            rows = self._query("SELECT * FROM params_history ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            out.append({"id": r["id"], "date": r["date"], "params": json.loads(r["params_json"]),
                        "source": r["source"],
                        "sweep_summary": json.loads(r["sweep_summary_json"]) if r["sweep_summary_json"] else None})
        return out

    def has_sweep_for(self, d: date | str) -> bool:
        """True if a nightly sweep already wrote its params_history row for session date `d`."""
        return self._one("SELECT 1 FROM params_history WHERE source='sweep' AND date=? LIMIT 1",
                         (_date_str(d),)) is not None

    def params_history_dates(self, source: str = "sweep") -> list[str]:
        return [r["date"] for r in self._query(
            "SELECT DISTINCT date FROM params_history WHERE source=? ORDER BY date", (source,))]

    # ---- controls ----------------------------------------------------------
    def read_controls(self) -> dict:
        r = self._one("SELECT * FROM controls WHERE id=1")
        if not r:
            return {"paused": False, "flatten_requested": False, "updated_at": None}
        return {"paused": bool(r["paused"]), "flatten_requested": bool(r["flatten_requested"]),
                "updated_at": r["updated_at"]}

    def _upsert_controls(self, now: datetime, **fields) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO controls(id,paused,flatten_requested,updated_at) VALUES(1,0,0,?)",
                               (iso(now),))
            sets = ", ".join(f"{k}=?" for k in fields)
            self._conn.execute(f"UPDATE controls SET {sets}, updated_at=? WHERE id=1",
                               tuple(int(v) for v in fields.values()) + (iso(now),))
            self._conn.commit()

    def set_paused(self, paused: bool, now: datetime) -> None:
        self._upsert_controls(now, paused=paused)

    def request_flatten(self, now: datetime) -> None:
        self._upsert_controls(now, flatten_requested=True)

    def clear_flatten_request(self, now: datetime) -> None:
        self._upsert_controls(now, flatten_requested=False)

    # ---- errors ------------------------------------------------------------
    def log_error(self, where: str, message: str, traceback: Optional[str], now: Optional[datetime] = None) -> int:
        now = now or datetime.now(ET)
        cur = self._exec("INSERT INTO errors(ts,where_,message,traceback) VALUES(?,?,?,?)",
                         (iso(now), where, message, traceback))
        return int(cur.lastrowid)

    def recent_errors(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))]

    def errors_count(self, where: Optional[str] = None, d: Optional[date | str] = None) -> int:
        sql, args = "SELECT COUNT(*) FROM errors WHERE 1=1", []
        if where is not None:
            sql += " AND where_=?"
            args.append(where)
        if d is not None:
            sql += " AND substr(ts,1,10)=?"
            args.append(_date_str(d))
        return int(self._one(sql, args)[0])
