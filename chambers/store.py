"""SQLite storage. All SQL lives in this module.

One file (data/chambers.db), WAL mode. The engine process is the single writer
of the trading tables; the dashboard writes only `controls`, `params` and lab
approvals; the night lab and the P100 replay write only their own tables.
All timestamps are stored as ISO-8601 strings with offset, in Eastern Time.

Schema versions (PRAGMA user_version):
  0  Phase 0 (SCHEMA_V0 below, unchanged so a fresh db and a migrated db end up identical)
  1  Phase 1A: sleeve_id / profile on cycles, signals, trades, params, params_history;
     per-sleeve heartbeat; new tables in MIGRATION_V1. An existing Phase 0 db is backed up
     (sqlite backup API) before it is migrated, and every Phase 0 row becomes sleeve S0 / profile LIVE.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
log = logging.getLogger("chambers.store")

SCHEMA_V0 = """
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

# Phase 1A. Statements run in order inside one transaction; see Store._migrate_v1.
MIGRATION_V1 = [
    # --- sleeve / profile columns (existing rows become S0 / LIVE through the defaults)
    "ALTER TABLE cycles ADD COLUMN sleeve_id TEXT NOT NULL DEFAULT 'S0'",
    "ALTER TABLE cycles ADD COLUMN profile TEXT NOT NULL DEFAULT 'LIVE'",
    "ALTER TABLE signals ADD COLUMN sleeve_id TEXT NOT NULL DEFAULT 'S0'",
    "ALTER TABLE signals ADD COLUMN profile TEXT NOT NULL DEFAULT 'LIVE'",
    "ALTER TABLE signals ADD COLUMN detail_json TEXT",
    "ALTER TABLE trades ADD COLUMN sleeve_id TEXT NOT NULL DEFAULT 'S0'",
    "ALTER TABLE trades ADD COLUMN profile TEXT NOT NULL DEFAULT 'LIVE'",
    "ALTER TABLE trades ADD COLUMN news_day INTEGER",
    "ALTER TABLE trades ADD COLUMN event TEXT",
    "ALTER TABLE params_history ADD COLUMN sleeve_id TEXT NOT NULL DEFAULT 'S0'",
    "ALTER TABLE params_history ADD COLUMN profile TEXT NOT NULL DEFAULT 'LIVE'",
    # --- params: one row per (sleeve, profile) instead of the id=1 singleton
    """CREATE TABLE params_v1(
         sleeve_id TEXT NOT NULL,
         profile TEXT NOT NULL,
         params_json TEXT NOT NULL,
         source TEXT NOT NULL,
         updated_at TEXT NOT NULL,
         PRIMARY KEY (sleeve_id, profile))""",
    "INSERT INTO params_v1(sleeve_id,profile,params_json,source,updated_at) "
    "SELECT 'S0','LIVE',params_json,source,updated_at FROM params",
    "DROP TABLE params",
    "ALTER TABLE params_v1 RENAME TO params",
    # --- heartbeat: one row per sleeve plus the overall row 'ALL'
    """CREATE TABLE heartbeat_v1(
         sleeve_id TEXT PRIMARY KEY,
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
         started_at TEXT)""",
    "INSERT INTO heartbeat_v1 SELECT 'S0',state,last_cycle_ts,cycles_today,signals_today,fired_today,opened_today,"
    "closed_today,open_positions,net_pnl_today,last_error,last_error_ts,pid,started_at FROM heartbeat",
    "DROP TABLE heartbeat",
    "ALTER TABLE heartbeat_v1 RENAME TO heartbeat",
    # --- indexes for per-sleeve reads
    "CREATE INDEX IF NOT EXISTS idx_cycles_sleeve_ts ON cycles(sleeve_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_signals_sleeve_ts ON signals(sleeve_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_trades_sleeve_exit ON trades(sleeve_id, profile, exit_ts)",
    "CREATE INDEX IF NOT EXISTS idx_params_history_sleeve ON params_history(sleeve_id, profile, source, date)",
    # --- new tables
    """CREATE TABLE sleeves(
         id TEXT PRIMARY KEY,
         name TEXT NOT NULL,
         strategy TEXT NOT NULL,
         symbols TEXT NOT NULL,          -- JSON list
         timeframe TEXT NOT NULL,
         capital REAL NOT NULL,          -- starting virtual capital; P&L accrues on top (Store.sleeve_equity)
         active INTEGER NOT NULL DEFAULT 1)""",
    # generic multi-timeframe bars (1-min bars stay in `bars`): '1h' (S2), 's4h' (S3), '1d' (lab, brief)
    """CREATE TABLE bars_tf(
         symbol TEXT NOT NULL,
         timeframe TEXT NOT NULL,
         ts TEXT NOT NULL,               -- bar start
         o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
         v REAL NOT NULL,
         PRIMARY KEY (symbol, timeframe, ts))""",
    "CREATE INDEX idx_bars_tf_ts ON bars_tf(timeframe, ts)",
    """CREATE TABLE twin_trades(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         sleeve_id TEXT NOT NULL,
         profile TEXT NOT NULL DEFAULT 'LIVE',
         day TEXT NOT NULL,              -- the twin day (seed day) this trade belongs to
         seed INTEGER NOT NULL,
         symbol TEXT NOT NULL,
         side TEXT NOT NULL,
         qty REAL NOT NULL,
         entry_ts TEXT NOT NULL,
         entry_price REAL NOT NULL,
         hypothesis_json TEXT,
         exit_ts TEXT,
         exit_price REAL,
         exit_reason TEXT,
         bars_held INTEGER NOT NULL DEFAULT 0,
         mae_pct REAL NOT NULL DEFAULT 0,
         mfe_pct REAL NOT NULL DEFAULT 0,
         gross_pnl REAL,
         est_cost REAL,
         net_pnl REAL,
         news_day INTEGER,
         event TEXT,
         state_json TEXT)""",
    "CREATE INDEX idx_twin_trades_sleeve_exit ON twin_trades(sleeve_id, exit_ts)",
    """CREATE TABLE twin_seeds(
         sleeve_id TEXT NOT NULL,
         day TEXT NOT NULL,
         seed INTEGER NOT NULL,
         p REAL NOT NULL,                -- entry probability per eligible evaluation
         p_source TEXT NOT NULL,
         created_at TEXT NOT NULL,
         PRIMARY KEY (sleeve_id, day))""",
    """CREATE TABLE recon(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         ts TEXT NOT NULL,
         kind TEXT NOT NULL,             -- 'equity_session' | 'crypto_rollover'
         equity REAL,
         prev_ts TEXT,
         prev_equity REAL,
         actual_delta REAL,
         realized_gross REAL,
         unrealized REAL,
         prev_unrealized REAL,
         fees REAL,
         expected_delta REAL,
         diff REAL,
         threshold REAL,
         status TEXT NOT NULL,           -- 'baseline' | 'pass' | 'mismatch' | 'error'
         detail_json TEXT)""",
    """CREATE TABLE alerts(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         ts TEXT NOT NULL,
         kind TEXT NOT NULL,             -- 'alert' | 'morning' | 'evening' | 'test'
         key TEXT NOT NULL,              -- identical alerts share a key (rate limit)
         message TEXT NOT NULL,
         status TEXT NOT NULL,           -- 'sent' | 'suppressed' | 'disabled' | 'failed' | 'dry_run'
         error TEXT)""",
    "CREATE INDEX idx_alerts_key_ts ON alerts(key, ts)",
    """CREATE TABLE econ_events(
         date TEXT NOT NULL,
         event TEXT NOT NULL,
         time TEXT,
         source TEXT,
         confirmed INTEGER,
         PRIMARY KEY (date, event))""",
    """CREATE TABLE lab_runs(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         started_at TEXT NOT NULL,
         finished_at TEXT,
         status TEXT NOT NULL,           -- 'running' | 'done' | 'failed'
         sessions_json TEXT,
         paused_s REAL NOT NULL DEFAULT 0,
         detail_json TEXT)""",
    """CREATE TABLE lab_results(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         run_id INTEGER NOT NULL,
         candidate TEXT NOT NULL,
         n_sessions INTEGER NOT NULL,
         train_dates_json TEXT,
         grade_dates_json TEXT,
         params_json TEXT,
         train_net REAL,
         grade_net REAL,
         grade_trades INTEGER,
         twin_grade_net REAL,
         twin_seed INTEGER,
         meets_rule INTEGER NOT NULL,
         reason TEXT,
         detail_json TEXT)""",
    """CREATE TABLE lab_suggestions(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         run_id INTEGER NOT NULL,
         result_id INTEGER NOT NULL,
         candidate TEXT NOT NULL,
         created_at TEXT NOT NULL,
         params_json TEXT,
         n_sessions INTEGER,
         grade_net REAL,
         twin_grade_net REAL,
         grade_trades INTEGER,
         status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'approved' | 'rejected' | 'superseded'
         decided_at TEXT,
         note TEXT)""",
    """CREATE TABLE p100_ledger(
         date TEXT PRIMARY KEY,
         start_equity REAL NOT NULL,
         end_equity REAL NOT NULL,
         settled_cash_start REAL NOT NULL,
         settled_cash_end REAL NOT NULL,
         unsettled_json TEXT NOT NULL,   -- [[amount, settle_date], ...] carried to later days
         trades INTEGER NOT NULL,
         skipped INTEGER NOT NULL,
         net_pnl REAL NOT NULL,
         shadow_trades INTEGER NOT NULL,
         shadow_net REAL NOT NULL,
         detail_json TEXT)""",
    """CREATE TABLE p100_trades(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         date TEXT NOT NULL,
         source TEXT NOT NULL,           -- 'S0' | 'S1' | lab candidate id
         symbol TEXT NOT NULL,           -- what P100 traded (SH/PSQ for a translated short)
         signal_symbol TEXT NOT NULL,    -- the symbol that produced the signal
         side TEXT NOT NULL,
         qty REAL NOT NULL,
         entry_ts TEXT NOT NULL,
         entry_price REAL NOT NULL,
         exit_ts TEXT,
         exit_price REAL,
         exit_reason TEXT,
         gross_pnl REAL,
         est_cost REAL,
         net_pnl REAL,
         learning_only INTEGER NOT NULL DEFAULT 0,   -- shadow shorts: reported, never in P100 equity
         news_day INTEGER,
         event TEXT,
         detail_json TEXT)""",
    "CREATE INDEX idx_p100_trades_date ON p100_trades(date)",
    """CREATE TABLE risk_days(
         date TEXT PRIMARY KEY,
         open_equity REAL,
         open_ts TEXT,
         halted INTEGER NOT NULL DEFAULT 0,
         halted_ts TEXT,
         detail_json TEXT)""",
]


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


def _j(x) -> Optional[str]:
    return json.dumps(x) if x is not None else None


def _uj(s: Optional[str]):
    return json.loads(s) if s else None


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
    qty: float
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
    sleeve_id: str = "S0"
    profile: str = "LIVE"
    news_day: Optional[int] = None
    event: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.exit_ts is None

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "long" else -self.qty

    @property
    def hypothesis(self) -> dict:
        return json.loads(self.hypothesis_json) if self.hypothesis_json else {}

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["hypothesis"] = json.loads(self.hypothesis_json) if self.hypothesis_json else None
        return d


TRADE_FIELDS = tuple(Trade.__dataclass_fields__)


class Store:
    """Typed access to the SQLite database. Thread-safe via a lock."""

    def __init__(self, path: str | os.PathLike = "data/chambers.db", backup_dir: str | os.PathLike | None = None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = Path(backup_dir) if backup_dir else (
            Path(self.path).parent / "backups" if self.path != ":memory:" else None)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migration_backup: Optional[str] = None
        self._ensure_schema()

    # ---- schema / migration -----------------------------------------------
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _ensure_schema(self) -> None:
        with self._lock:
            had_data = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'").fetchone() is not None
            self._conn.executescript(SCHEMA_V0)   # no-op on an existing db (IF NOT EXISTS)
            self._conn.commit()
            if self.schema_version() < 1:
                if had_data and self.path != ":memory:":
                    self.migration_backup = self._backup_before_migration()
                self._migrate_v1()

    def _backup_before_migration(self) -> str:
        """Copy the Phase 0 db with the sqlite backup API before touching its schema."""
        stamp = datetime.now(ET).strftime("%Y%m%d-%H%M%S")
        dest_dir = self.backup_dir or Path(self.path).parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"pre-phase1a-{stamp}.db"
        n = 1
        while dest.exists():
            dest = dest_dir / f"pre-phase1a-{stamp}-{n}.db"
            n += 1
        dst = sqlite3.connect(str(dest))
        try:
            self._conn.backup(dst)
        finally:
            dst.close()
        log.warning("Phase 0 database backed up to %s before the Phase 1A migration", dest)
        return str(dest)

    def _migrate_v1(self) -> None:
        conn = self._conn
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in MIGRATION_V1:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, dest: str | os.PathLike) -> str:
        """Online copy with the sqlite backup API (safe while the engine writes)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        dst = sqlite3.connect(str(tmp))
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, dest)
        return str(dest)

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

    # ---- sleeves -----------------------------------------------------------
    def upsert_sleeve(self, sleeve_id: str, name: str, strategy: str, symbols: list[str], timeframe: str,
                      capital: float, active: bool = True) -> None:
        self._exec(
            "INSERT INTO sleeves(id,name,strategy,symbols,timeframe,capital,active) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, strategy=excluded.strategy,"
            " symbols=excluded.symbols, timeframe=excluded.timeframe, capital=excluded.capital,"
            " active=excluded.active",
            (sleeve_id, name, strategy, json.dumps(symbols), timeframe, float(capital), int(active)))

    def sleeves(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM sleeves" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        out = []
        for r in self._query(sql):
            d = dict(r)
            d["symbols"] = json.loads(d["symbols"])
            d["active"] = bool(d["active"])
            out.append(d)
        return out

    def get_sleeve(self, sleeve_id: str) -> Optional[dict]:
        return next((s for s in self.sleeves() if s["id"] == sleeve_id), None)

    def realized_net(self, sleeve_id: str, profile: str = "LIVE", before: Optional[str] = None) -> float:
        sql = "SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE sleeve_id=? AND profile=? AND exit_ts IS NOT NULL"
        args: list = [sleeve_id, profile]
        if before is not None:
            sql += " AND exit_ts < ?"
            args.append(before)
        return float(self._one(sql, args)[0])

    def sleeve_equity(self, sleeve_id: str) -> Optional[float]:
        """Starting virtual capital + every closed trade's net P&L for the sleeve."""
        s = self.get_sleeve(sleeve_id)
        if s is None:
            return None
        return s["capital"] + self.realized_net(sleeve_id)

    # ---- cycles ------------------------------------------------------------
    def write_cycle(self, ts: datetime, state: str, symbols_evaluated: int, signals_fired: int,
                    orders_placed: int, errors: int, duration_ms: int, sleeve_id: str = "S0",
                    profile: str = "LIVE") -> int:
        cur = self._exec(
            "INSERT INTO cycles(ts,state,symbols_evaluated,signals_fired,orders_placed,errors,duration_ms,"
            "sleeve_id,profile) VALUES(?,?,?,?,?,?,?,?,?)",
            (iso(ts), state, symbols_evaluated, signals_fired, orders_placed, errors, duration_ms, sleeve_id, profile))
        return int(cur.lastrowid)

    def cycles_for_day(self, d: date | str, sleeve_id: str = "S0") -> list[dict]:
        rows = self._query("SELECT * FROM cycles WHERE sleeve_id=? AND substr(ts,1,10)=? ORDER BY ts",
                           (sleeve_id, _date_str(d)))
        return [dict(r) for r in rows]

    def cycles_between(self, sleeve_id: str, start: datetime, end: datetime) -> list[dict]:
        rows = self._query("SELECT * FROM cycles WHERE sleeve_id=? AND ts>=? AND ts<? ORDER BY ts",
                           (sleeve_id, iso(start), iso(end)))
        return [dict(r) for r in rows]

    def cycle_dates(self, limit: int = 5, sleeve_id: str = "S0") -> list[str]:
        """The last `limit` dates with at least one `running` cycle (a `--once` or after-hours start isn't a session)."""
        rows = self._query(
            "SELECT DISTINCT substr(ts,1,10) AS d FROM cycles WHERE sleeve_id=? AND state='running' "
            "ORDER BY d DESC LIMIT ?", (sleeve_id, limit))
        return sorted(r["d"] for r in rows)

    def max_cycle_duration_since(self, since: datetime) -> int:
        """Longest cycle (ms) of any sleeve since `since` — the night lab's pause signal."""
        r = self._one("SELECT MAX(duration_ms) FROM cycles WHERE ts>=?", (iso(since),))
        return int(r[0] or 0)

    def last_cycle(self, sleeve_id: str) -> Optional[dict]:
        r = self._one("SELECT * FROM cycles WHERE sleeve_id=? ORDER BY id DESC LIMIT 1", (sleeve_id,))
        return dict(r) if r else None

    # ---- signals -----------------------------------------------------------
    def write_signals(self, cycle_id: Optional[int], rows: list[dict], sleeve_id: str = "S0",
                      profile: str = "LIVE") -> None:
        """rows: dicts with ts(datetime), symbol, close, vwap, dev_pct, vol_ratio, side, fired, reason, params(dict),
        and optionally detail (dict, sleeve-specific numbers such as z or ATR)."""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO signals(cycle_id,ts,symbol,close,vwap,dev_pct,vol_ratio,side,fired,reason,params_json,"
                "sleeve_id,profile,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(cycle_id, iso(r["ts"]), r["symbol"], r.get("close"), r.get("vwap"), r.get("dev_pct"),
                  r.get("vol_ratio"), r.get("side"), 1 if r.get("fired") else 0, r["reason"],
                  json.dumps(r["params"]) if r.get("params") is not None else None, sleeve_id, profile,
                  _j(r.get("detail"))) for r in rows])
            self._conn.commit()

    def write_signal(self, cycle_id: Optional[int], **row) -> None:
        self.write_signals(cycle_id, [row])

    def signals_for_day(self, d: date | str, sleeve_id: str = "S0") -> list[dict]:
        rows = self._query("SELECT * FROM signals WHERE sleeve_id=? AND substr(ts,1,10)=? ORDER BY id",
                           (sleeve_id, _date_str(d)))
        return [dict(r) for r in rows]

    def signals_count_for_day(self, d: date | str, fired_only: bool = False, sleeve_id: str = "S0") -> int:
        sql = "SELECT COUNT(*) FROM signals WHERE sleeve_id=? AND substr(ts,1,10)=?"
        if fired_only:
            sql += " AND fired=1"
        return int(self._one(sql, (sleeve_id, _date_str(d)))[0])

    def signal_dates(self, sleeve_id: str, before: date | str, limit: int = 20) -> list[str]:
        rows = self._query(
            "SELECT DISTINCT substr(ts,1,10) AS d FROM signals WHERE sleeve_id=? AND substr(ts,1,10)<? "
            "ORDER BY d DESC LIMIT ?", (sleeve_id, _date_str(before), limit))
        return sorted(r["d"] for r in rows)

    def signal_rate(self, sleeve_id: str, dates: list[str], not_eligible: Iterable[str]) -> tuple[int, int]:
        """(fired, eligible evaluations) over `dates`; an evaluation is eligible unless its reason is in
        `not_eligible` (entries closed, not enough bars, already holding, cooling down)."""
        if not dates:
            return 0, 0
        ne = list(not_eligible)
        q_d = ",".join("?" * len(dates))
        q_r = ",".join("?" * len(ne)) or "''"
        r = self._one(
            f"SELECT COALESCE(SUM(fired),0), COUNT(*) FROM signals WHERE sleeve_id=? AND substr(ts,1,10) IN ({q_d})"
            f" AND reason NOT IN ({q_r})", [sleeve_id, *dates, *ne])
        return int(r[0]), int(r[1])

    # ---- trades ------------------------------------------------------------
    def open_trade(self, symbol: str, side: str, qty: float, entry_ts: datetime, entry_price: float,
                   entry_order_id: Optional[str], entry_bid: Optional[float], entry_ask: Optional[float],
                   hypothesis: dict, params: dict, sleeve_id: str = "S0", profile: str = "LIVE",
                   news_day: Optional[bool] = None, event: Optional[str] = None) -> int:
        cur = self._exec(
            "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
            "hypothesis_json,params_json,sleeve_id,profile,news_day,event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, side, qty, iso(entry_ts), entry_price, entry_order_id, entry_bid, entry_ask,
             json.dumps(hypothesis), json.dumps(params), sleeve_id, profile,
             None if news_day is None else int(bool(news_day)), event))
        return int(cur.lastrowid)

    def split_trade(self, trade_id: int, keep_qty: float) -> int:
        """Shrink open trade `trade_id` to `keep_qty` and insert an identical open trade for the rest
        (a partially filled exit). Returns the new trade's id."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
                "hypothesis_json,bars_held,mae_pct,mfe_pct,params_json,sleeve_id,profile,news_day,event) "
                "SELECT symbol,side,qty-?,entry_ts,entry_price,entry_order_id,entry_bid,entry_ask,"
                "hypothesis_json,bars_held,mae_pct,mfe_pct,params_json,sleeve_id,profile,news_day,event "
                "FROM trades WHERE id=?", (keep_qty, trade_id))
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

    def all_closed_trades(self, sleeve_id: Optional[str] = "S0") -> list[Trade]:
        if sleeve_id is None:
            return [self._trade(r) for r in self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL ORDER BY id")]
        return [self._trade(r) for r in self._query(
            "SELECT * FROM trades WHERE exit_ts IS NOT NULL AND sleeve_id=? ORDER BY id", (sleeve_id,))]

    def _trade(self, r: sqlite3.Row) -> Trade:
        return Trade(**{k: r[k] for k in r.keys() if k in TRADE_FIELDS})

    def get_trade(self, trade_id: int) -> Optional[Trade]:
        r = self._one("SELECT * FROM trades WHERE id=?", (trade_id,))
        return self._trade(r) if r else None

    def open_trades(self, sleeve_id: Optional[str] = "S0", profile: str = "LIVE") -> list[Trade]:
        """Open trades of one sleeve (default S0, as in Phase 0); sleeve_id=None → every sleeve."""
        if sleeve_id is None:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NULL AND profile=? ORDER BY id", (profile,))
        else:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NULL AND sleeve_id=? AND profile=? ORDER BY id",
                               (sleeve_id, profile))
        return [self._trade(r) for r in rows]

    def closed_trades_for_day(self, d: date | str, sleeve_id: Optional[str] = "S0") -> list[Trade]:
        if sleeve_id is None:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL AND profile='LIVE' "
                               "AND substr(exit_ts,1,10)=? ORDER BY exit_ts", (_date_str(d),))
        else:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL AND sleeve_id=? "
                               "AND substr(exit_ts,1,10)=? ORDER BY exit_ts", (sleeve_id, _date_str(d)))
        return [self._trade(r) for r in rows]

    def closed_trades_between(self, sleeve_id: Optional[str], start: datetime, end: datetime) -> list[Trade]:
        """Closed LIVE trades with start <= exit_ts < end (all sleeves when sleeve_id is None)."""
        sql = "SELECT * FROM trades WHERE exit_ts IS NOT NULL AND profile='LIVE' AND exit_ts>=? AND exit_ts<?"
        args: list = [iso(start), iso(end)]
        if sleeve_id is not None:
            sql += " AND sleeve_id=?"
            args.append(sleeve_id)
        return [self._trade(r) for r in self._query(sql + " ORDER BY exit_ts", args)]

    def recent_closed_trades(self, limit: int = 25, sleeve_id: Optional[str] = "S0") -> list[Trade]:
        if sleeve_id is None:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL AND profile='LIVE' "
                               "ORDER BY exit_ts DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._query("SELECT * FROM trades WHERE exit_ts IS NOT NULL AND sleeve_id=? "
                               "ORDER BY exit_ts DESC, id DESC LIMIT ?", (sleeve_id, limit))
        return [self._trade(r) for r in rows]

    def opened_count_for_day(self, d: date | str, sleeve_id: str = "S0") -> int:
        return int(self._one("SELECT COUNT(*) FROM trades WHERE sleeve_id=? AND substr(entry_ts,1,10)=?",
                             (sleeve_id, _date_str(d)))[0])

    def trade_dates(self, sleeve_id: str, limit: int = 20) -> list[str]:
        rows = self._query("SELECT DISTINCT substr(exit_ts,1,10) AS d FROM trades WHERE sleeve_id=? "
                           "AND exit_ts IS NOT NULL ORDER BY d DESC LIMIT ?", (sleeve_id, limit))
        return sorted(r["d"] for r in rows)

    def set_news_tags_for_open(self, trade_id: int, news_day: bool, event: Optional[str]) -> None:
        self._exec("UPDATE trades SET news_day=?, event=? WHERE id=?", (int(bool(news_day)), event, trade_id))

    # ---- bars (1-min) ------------------------------------------------------
    def write_bars(self, symbol: str, bars: Iterable[Bar]) -> int:
        rows = [(symbol, iso(b.ts), b.o, b.h, b.l, b.c, b.v) for b in bars]
        if not rows:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO bars(symbol,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", rows)
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _bars_out(rows) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}
        for r in rows:
            out.setdefault(r["symbol"], []).append(Bar(parse_ts(r["ts"]), r["o"], r["h"], r["l"], r["c"], r["v"]))
        return out

    def bars_for_day(self, d: date | str, symbol: Optional[str] = None,
                     symbols: Optional[Iterable[str]] = None) -> dict[str, list[Bar]]:
        if symbol is not None:
            symbols = [symbol]
        if symbols is None:
            rows = self._query("SELECT * FROM bars WHERE substr(ts,1,10)=? ORDER BY symbol, ts", (_date_str(d),))
        else:
            syms = list(symbols)
            if not syms:
                return {}
            q = ",".join("?" * len(syms))
            rows = self._query(f"SELECT * FROM bars WHERE substr(ts,1,10)=? AND symbol IN ({q}) ORDER BY symbol, ts",
                               (_date_str(d), *syms))
        return self._bars_out(rows)

    def bars_between(self, symbols: Iterable[str], start: datetime, end: datetime) -> dict[str, list[Bar]]:
        """1-min bars with start <= ts < end."""
        syms = list(symbols)
        if not syms:
            return {}
        q = ",".join("?" * len(syms))
        rows = self._query(f"SELECT * FROM bars WHERE symbol IN ({q}) AND ts>=? AND ts<? ORDER BY symbol, ts",
                           (*syms, iso(start), iso(end)))
        return self._bars_out(rows)

    def bar_dates(self, limit: int = 5, symbols: Optional[Iterable[str]] = None,
                  before: Optional[date | str] = None) -> list[str]:
        sql, args = "SELECT DISTINCT substr(ts,1,10) AS d FROM bars WHERE 1=1", []
        if symbols is not None:
            syms = list(symbols)
            sql += f" AND symbol IN ({','.join('?' * len(syms))})"
            args += syms
        if before is not None:
            sql += " AND substr(ts,1,10)<?"
            args.append(_date_str(before))
        rows = self._query(sql + " ORDER BY d DESC LIMIT ?", (*args, limit))
        return sorted(r["d"] for r in rows)

    def latest_bar_ts(self, d: date | str, symbols: Optional[Iterable[str]] = None) -> Optional[str]:
        if symbols is None:
            r = self._one("SELECT MAX(ts) FROM bars WHERE substr(ts,1,10)=?", (_date_str(d),))
        else:
            syms = list(symbols)
            r = self._one(f"SELECT MAX(ts) FROM bars WHERE substr(ts,1,10)=? AND symbol IN ({','.join('?' * len(syms))})",
                          (_date_str(d), *syms))
        return r[0] if r and r[0] else None

    def bar_count_for_day(self, d: date | str, symbol: str) -> int:
        return int(self._one("SELECT COUNT(*) FROM bars WHERE symbol=? AND substr(ts,1,10)=?",
                             (symbol, _date_str(d)))[0])

    # ---- bars (other timeframes) ---------------------------------------------
    def write_bars_tf(self, symbol: str, timeframe: str, bars: Iterable[Bar], replace: bool = False) -> int:
        rows = [(symbol, timeframe, iso(b.ts), b.o, b.h, b.l, b.c, b.v) for b in bars]
        if not rows:
            return 0
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        with self._lock:
            cur = self._conn.executemany(
                f"{verb} INTO bars_tf(symbol,timeframe,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
            return cur.rowcount

    def bars_tf(self, symbol: str, timeframe: str, start: Optional[datetime] = None,
                end: Optional[datetime] = None, limit: Optional[int] = None) -> list[Bar]:
        """Bars in time order with start <= ts < end; `limit` keeps the most recent ones."""
        sql, args = "SELECT * FROM bars_tf WHERE symbol=? AND timeframe=?", [symbol, timeframe]
        if start is not None:
            sql += " AND ts>=?"
            args.append(iso(start))
        if end is not None:
            sql += " AND ts<?"
            args.append(iso(end))
        if limit is not None:
            rows = self._query(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))[::-1]
        else:
            rows = self._query(sql + " ORDER BY ts", args)
        return [Bar(parse_ts(r["ts"]), r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]

    def last_bar_tf_ts(self, symbol: str, timeframe: str) -> Optional[datetime]:
        r = self._one("SELECT MAX(ts) FROM bars_tf WHERE symbol=? AND timeframe=?", (symbol, timeframe))
        return parse_ts(r[0]) if r and r[0] else None

    def count_bars_tf(self, symbol: str, timeframe: str) -> int:
        return int(self._one("SELECT COUNT(*) FROM bars_tf WHERE symbol=? AND timeframe=?", (symbol, timeframe))[0])

    # ---- heartbeat ---------------------------------------------------------
    HEARTBEAT_FIELDS = ("state", "last_cycle_ts", "cycles_today", "signals_today", "fired_today",
                        "opened_today", "closed_today", "open_positions", "net_pnl_today",
                        "last_error", "last_error_ts", "pid", "started_at")

    def write_heartbeat(self, sleeve_id: str = "S0", **fields) -> None:
        """Upsert one sleeve's heartbeat row ('ALL' is the overall row); only the given fields change."""
        bad = set(fields) - set(self.HEARTBEAT_FIELDS)
        if bad:
            raise ValueError(f"unknown heartbeat fields: {bad}")
        for k in ("last_cycle_ts", "last_error_ts", "started_at"):
            if isinstance(fields.get(k), datetime):
                fields[k] = iso(fields[k])
        with self._lock:
            cur = self._conn.execute("SELECT sleeve_id FROM heartbeat WHERE sleeve_id=?", (sleeve_id,)).fetchone()
            if cur is None:
                self._conn.execute("INSERT INTO heartbeat(sleeve_id,state) VALUES(?,?)",
                                   (sleeve_id, fields.get("state", "idle")))
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(f"UPDATE heartbeat SET {sets} WHERE sleeve_id=?", (*fields.values(), sleeve_id))
            self._conn.commit()

    def read_heartbeat(self, sleeve_id: str = "S0") -> Optional[dict]:
        r = self._one("SELECT * FROM heartbeat WHERE sleeve_id=?", (sleeve_id,))
        return dict(r) if r else None

    def read_heartbeats(self) -> dict[str, dict]:
        return {r["sleeve_id"]: dict(r) for r in self._query("SELECT * FROM heartbeat ORDER BY sleeve_id")}

    # ---- params ------------------------------------------------------------
    def read_params(self, sleeve_id: str = "S0", profile: str = "LIVE") -> Optional[dict]:
        r = self._one("SELECT * FROM params WHERE sleeve_id=? AND profile=?", (sleeve_id, profile))
        if not r:
            return None
        return {"params": json.loads(r["params_json"]), "source": r["source"], "updated_at": r["updated_at"]}

    def write_params(self, params: dict, source: str, now: datetime, sleeve_id: str = "S0",
                     profile: str = "LIVE") -> None:
        self._exec(
            "INSERT INTO params(sleeve_id,profile,params_json,source,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(sleeve_id,profile) DO UPDATE SET params_json=excluded.params_json, source=excluded.source,"
            " updated_at=excluded.updated_at",
            (sleeve_id, profile, json.dumps(params), source, iso(now)))

    def write_params_history(self, d: date | str, params: dict, source: str,
                             sweep_summary: Optional[dict], sleeve_id: str = "S0", profile: str = "LIVE") -> int:
        cur = self._exec(
            "INSERT INTO params_history(date,params_json,source,sweep_summary_json,sleeve_id,profile)"
            " VALUES(?,?,?,?,?,?)",
            (_date_str(d), json.dumps(params), source,
             json.dumps(sweep_summary) if sweep_summary is not None else None, sleeve_id, profile))
        return int(cur.lastrowid)

    def params_history(self, limit: int = 7, source: Optional[str] = None, sleeve_id: str = "S0",
                       profile: str = "LIVE") -> list[dict]:
        sql, args = "SELECT * FROM params_history WHERE sleeve_id=? AND profile=?", [sleeve_id, profile]
        if source:
            sql += " AND source=?"
            args.append(source)
        rows = self._query(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        out = []
        for r in rows:
            out.append({"id": r["id"], "date": r["date"], "params": json.loads(r["params_json"]),
                        "source": r["source"], "sleeve_id": r["sleeve_id"], "profile": r["profile"],
                        "sweep_summary": json.loads(r["sweep_summary_json"]) if r["sweep_summary_json"] else None})
        return out

    def has_sweep_for(self, d: date | str, sleeve_id: str = "S0", profile: str = "LIVE") -> bool:
        """True if a nightly sweep already wrote its params_history row for session date `d`."""
        return self._one("SELECT 1 FROM params_history WHERE source='sweep' AND date=? AND sleeve_id=? AND profile=?"
                         " LIMIT 1", (_date_str(d), sleeve_id, profile)) is not None

    def params_history_dates(self, source: str = "sweep", sleeve_id: str = "S0", profile: str = "LIVE") -> list[str]:
        return [r["date"] for r in self._query(
            "SELECT DISTINCT date FROM params_history WHERE source=? AND sleeve_id=? AND profile=? ORDER BY date",
            (source, sleeve_id, profile))]

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

    def errors_count(self, where: Optional[str] = None, d: Optional[date | str] = None,
                     where_like: Optional[str] = None) -> int:
        sql, args = "SELECT COUNT(*) FROM errors WHERE 1=1", []
        if where is not None:
            sql += " AND where_=?"
            args.append(where)
        if where_like is not None:
            sql += " AND where_ LIKE ?"
            args.append(where_like)
        if d is not None:
            sql += " AND substr(ts,1,10)=?"
            args.append(_date_str(d))
        return int(self._one(sql, args)[0])

    def errors_since(self, since: datetime, where: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM errors WHERE ts>=?", [iso(since)]
        if where is not None:
            sql += " AND where_=?"
            args.append(where)
        return [dict(r) for r in self._query(sql + " ORDER BY id", args)]

    # ---- twins -------------------------------------------------------------
    def get_twin_seed(self, sleeve_id: str, day: str) -> Optional[dict]:
        r = self._one("SELECT * FROM twin_seeds WHERE sleeve_id=? AND day=?", (sleeve_id, day))
        return dict(r) if r else None

    def set_twin_seed(self, sleeve_id: str, day: str, seed: int, p: float, p_source: str, now: datetime) -> None:
        self._exec("INSERT OR IGNORE INTO twin_seeds(sleeve_id,day,seed,p,p_source,created_at) VALUES(?,?,?,?,?,?)",
                   (sleeve_id, day, int(seed), float(p), p_source, iso(now)))

    def open_twin_trade(self, sleeve_id: str, day: str, seed: int, symbol: str, side: str, qty: float,
                        entry_ts: datetime, entry_price: float, hypothesis: dict, state: dict,
                        news_day: bool, event: Optional[str], profile: str = "LIVE") -> int:
        cur = self._exec(
            "INSERT INTO twin_trades(sleeve_id,profile,day,seed,symbol,side,qty,entry_ts,entry_price,hypothesis_json,"
            "state_json,news_day,event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sleeve_id, profile, day, int(seed), symbol, side, qty, iso(entry_ts), entry_price, json.dumps(hypothesis),
             json.dumps(state), int(bool(news_day)), event))
        return int(cur.lastrowid)

    def update_twin_progress(self, twin_id: int, bars_held: int, mae_pct: float, mfe_pct: float,
                             state: dict) -> None:
        self._exec("UPDATE twin_trades SET bars_held=?, mae_pct=?, mfe_pct=?, state_json=? WHERE id=?",
                   (bars_held, mae_pct, mfe_pct, json.dumps(state), twin_id))

    def close_twin_trade(self, twin_id: int, exit_ts: datetime, exit_price: float, exit_reason: str,
                         bars_held: int, mae_pct: float, mfe_pct: float, gross: float, cost: float,
                         net: float) -> None:
        self._exec("UPDATE twin_trades SET exit_ts=?, exit_price=?, exit_reason=?, bars_held=?, mae_pct=?, mfe_pct=?,"
                   " gross_pnl=?, est_cost=?, net_pnl=? WHERE id=?",
                   (iso(exit_ts), exit_price, exit_reason, bars_held, mae_pct, mfe_pct, gross, cost, net, twin_id))

    def open_twin_trades(self, sleeve_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM twin_trades WHERE sleeve_id=? AND exit_ts IS NULL ORDER BY id", (sleeve_id,))]

    def twin_trades_for_day(self, d: date | str, sleeve_id: str, closed_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM twin_trades WHERE sleeve_id=? AND substr(exit_ts,1,10)=?"
        return [dict(r) for r in self._query(sql + " ORDER BY exit_ts", (sleeve_id, _date_str(d)))]

    def twin_net_between(self, sleeve_id: str, start_day: str, end_day: str) -> float:
        r = self._one("SELECT COALESCE(SUM(net_pnl),0) FROM twin_trades WHERE sleeve_id=? AND exit_ts IS NOT NULL "
                      "AND substr(exit_ts,1,10)>=? AND substr(exit_ts,1,10)<=?", (sleeve_id, start_day, end_day))
        return float(r[0])

    def sleeve_net_between(self, sleeve_id: str, start_day: str, end_day: str) -> float:
        r = self._one("SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE sleeve_id=? AND profile='LIVE' AND "
                      "exit_ts IS NOT NULL AND substr(exit_ts,1,10)>=? AND substr(exit_ts,1,10)<=?",
                      (sleeve_id, start_day, end_day))
        return float(r[0])

    def all_closed_twin_trades(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM twin_trades WHERE exit_ts IS NOT NULL ORDER BY id")]

    # ---- recon -------------------------------------------------------------
    def write_recon(self, ts: datetime, kind: str, status: str, **fields) -> int:
        cols = ["ts", "kind", "status"] + list(fields)
        vals = [iso(ts), kind, status] + [
            _j(v) if k == "detail_json" else (iso(v) if isinstance(v, datetime) else v) for k, v in fields.items()]
        cur = self._exec(f"INSERT INTO recon({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)

    def last_recon(self, kind: Optional[str] = None, with_equity: bool = True) -> Optional[dict]:
        sql, args = "SELECT * FROM recon WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if with_equity:
            sql += " AND equity IS NOT NULL"
        r = self._one(sql + " ORDER BY id DESC LIMIT 1", args)
        return self._recon_row(r) if r else None

    def recent_recon(self, limit: int = 10) -> list[dict]:
        return [self._recon_row(r) for r in self._query("SELECT * FROM recon ORDER BY id DESC LIMIT ?", (limit,))]

    def recon_for_day(self, d: date | str) -> list[dict]:
        return [self._recon_row(r) for r in self._query("SELECT * FROM recon WHERE substr(ts,1,10)=? ORDER BY id",
                                                         (_date_str(d),))]

    @staticmethod
    def _recon_row(r) -> dict:
        d = dict(r)
        d["detail"] = _uj(d.pop("detail_json"))
        return d

    # ---- alerts ------------------------------------------------------------
    def write_alert(self, ts: datetime, kind: str, key: str, message: str, status: str,
                    error: Optional[str] = None) -> int:
        cur = self._exec("INSERT INTO alerts(ts,kind,key,message,status,error) VALUES(?,?,?,?,?,?)",
                         (iso(ts), kind, key, message, status, error))
        return int(cur.lastrowid)

    def last_alert(self, key: str, statuses: Iterable[str] = ("sent", "disabled", "dry_run")) -> Optional[dict]:
        st = list(statuses)
        r = self._one(f"SELECT * FROM alerts WHERE key=? AND status IN ({','.join('?' * len(st))}) "
                      "ORDER BY id DESC LIMIT 1", (key, *st))
        return dict(r) if r else None

    def recent_alerts(self, limit: int = 20, kind: Optional[str] = None) -> list[dict]:
        if kind:
            return [dict(r) for r in self._query("SELECT * FROM alerts WHERE kind=? ORDER BY id DESC LIMIT ?",
                                                 (kind, limit))]
        return [dict(r) for r in self._query("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))]

    def alerts_since(self, since: datetime, kind: str = "alert") -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM alerts WHERE kind=? AND ts>=? ORDER BY id",
                                             (kind, iso(since)))]

    def alert_days(self, kind: str, statuses: Iterable[str] = ("sent",)) -> list[str]:
        st = list(statuses)
        return [r["d"] for r in self._query(
            f"SELECT DISTINCT substr(ts,1,10) AS d FROM alerts WHERE kind=? AND status IN ({','.join('?' * len(st))})"
            " ORDER BY d", (kind, *st))]

    # ---- econ calendar -------------------------------------------------------
    def replace_econ_events(self, events: list[dict]) -> int:
        with self._lock:
            self._conn.execute("DELETE FROM econ_events")
            self._conn.executemany(
                "INSERT OR REPLACE INTO econ_events(date,event,time,source,confirmed) VALUES(?,?,?,?,?)",
                [(_date_str(e["date"]), e["event"], e.get("time"), e.get("source"),
                  None if e.get("confirmed") is None else int(bool(e["confirmed"]))) for e in events])
            self._conn.commit()
        return len(events)

    def econ_events_on(self, d: date | str) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM econ_events WHERE date=? ORDER BY time, event",
                                             (_date_str(d),))]

    def econ_events_between(self, start: date | str, end: date | str) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM econ_events WHERE date>=? AND date<=? ORDER BY date, time",
                                             (_date_str(start), _date_str(end)))]

    def backfill_news_tags(self) -> int:
        """Tag trades / twin trades that have no news_day yet from econ_events (by ET entry date)."""
        n = 0
        with self._lock:
            for table in ("trades", "twin_trades", "p100_trades"):
                cur = self._conn.execute(
                    f"UPDATE {table} SET news_day = CASE WHEN EXISTS (SELECT 1 FROM econ_events e "
                    f"WHERE e.date=substr({table}.entry_ts,1,10)) THEN 1 ELSE 0 END, "
                    f"event = (SELECT group_concat(e.event, '; ') FROM econ_events e "
                    f"WHERE e.date=substr({table}.entry_ts,1,10)) WHERE news_day IS NULL")
                n += cur.rowcount
            self._conn.commit()
        return n

    # ---- night lab -----------------------------------------------------------
    def start_lab_run(self, now: datetime, sessions: list[str]) -> int:
        cur = self._exec("INSERT INTO lab_runs(started_at,status,sessions_json) VALUES(?,?,?)",
                         (iso(now), "running", json.dumps(sessions)))
        return int(cur.lastrowid)

    def finish_lab_run(self, run_id: int, now: datetime, status: str, paused_s: float, detail: dict) -> None:
        self._exec("UPDATE lab_runs SET finished_at=?, status=?, paused_s=?, detail_json=? WHERE id=?",
                   (iso(now), status, paused_s, json.dumps(detail), run_id))

    def lab_runs(self, limit: int = 10) -> list[dict]:
        out = []
        for r in self._query("SELECT * FROM lab_runs ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["sessions"] = _uj(d.pop("sessions_json"))
            d["detail"] = _uj(d.pop("detail_json"))
            out.append(d)
        return out

    def write_lab_result(self, run_id: int, candidate: str, n_sessions: int, train_dates: list[str],
                         grade_dates: list[str], params: Optional[dict], train_net: Optional[float],
                         grade_net: Optional[float], grade_trades: Optional[int], twin_grade_net: Optional[float],
                         twin_seed: Optional[int], meets_rule: bool, reason: str, detail: Optional[dict]) -> int:
        cur = self._exec(
            "INSERT INTO lab_results(run_id,candidate,n_sessions,train_dates_json,grade_dates_json,params_json,"
            "train_net,grade_net,grade_trades,twin_grade_net,twin_seed,meets_rule,reason,detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, candidate, n_sessions, json.dumps(train_dates), json.dumps(grade_dates), _j(params), train_net,
             grade_net, grade_trades, twin_grade_net, twin_seed, int(meets_rule), reason, _j(detail)))
        return int(cur.lastrowid)

    def lab_results(self, run_id: Optional[int] = None) -> list[dict]:
        if run_id is None:
            r = self._one("SELECT MAX(run_id) FROM lab_results")
            run_id = r[0] if r and r[0] is not None else -1
        out = []
        for r in self._query("SELECT * FROM lab_results WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            for k in ("train_dates_json", "grade_dates_json", "params_json", "detail_json"):
                d[k[:-5]] = _uj(d.pop(k))
            d["meets_rule"] = bool(d["meets_rule"])
            out.append(d)
        return out

    def write_lab_suggestion(self, run_id: int, result_id: int, candidate: str, now: datetime, params: Optional[dict],
                             n_sessions: int, grade_net: float, twin_grade_net: float, grade_trades: int) -> int:
        with self._lock:
            # a newer suggestion for the same candidate supersedes an older undecided one
            self._conn.execute("UPDATE lab_suggestions SET status='superseded' WHERE candidate=? AND status='pending'",
                               (candidate,))
            cur = self._conn.execute(
                "INSERT INTO lab_suggestions(run_id,result_id,candidate,created_at,params_json,n_sessions,grade_net,"
                "twin_grade_net,grade_trades,status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                (run_id, result_id, candidate, iso(now), _j(params), n_sessions, grade_net, twin_grade_net,
                 grade_trades))
            self._conn.commit()
            return int(cur.lastrowid)

    def lab_suggestions(self, statuses: Optional[Iterable[str]] = None, limit: int = 20) -> list[dict]:
        sql, args = "SELECT * FROM lab_suggestions WHERE 1=1", []
        if statuses is not None:
            st = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        out = []
        for r in self._query(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)):
            d = dict(r)
            d["params"] = _uj(d.pop("params_json"))
            out.append(d)
        return out

    def decide_lab_suggestion(self, suggestion_id: int, status: str, now: datetime, note: Optional[str] = None) -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved or rejected")
        cur = self._exec("UPDATE lab_suggestions SET status=?, decided_at=?, note=? WHERE id=? AND status='pending'",
                         (status, iso(now), note, suggestion_id))
        return cur.rowcount == 1

    def latest_lab_rule_passes(self) -> list[dict]:
        """Candidates whose latest lab result meets the §7 suggestion rule (P100 may use them)."""
        return [r for r in self.lab_results() if r["meets_rule"]]

    # ---- P100 --------------------------------------------------------------
    def p100_last_ledger(self, before: Optional[date | str] = None) -> Optional[dict]:
        if before is None:
            r = self._one("SELECT * FROM p100_ledger ORDER BY date DESC LIMIT 1")
        else:
            r = self._one("SELECT * FROM p100_ledger WHERE date<? ORDER BY date DESC LIMIT 1", (_date_str(before),))
        return self._ledger_row(r) if r else None

    def p100_ledger(self, limit: int = 20) -> list[dict]:
        return [self._ledger_row(r) for r in self._query("SELECT * FROM p100_ledger ORDER BY date DESC LIMIT ?",
                                                         (limit,))]

    def p100_ledger_for(self, d: date | str) -> Optional[dict]:
        r = self._one("SELECT * FROM p100_ledger WHERE date=?", (_date_str(d),))
        return self._ledger_row(r) if r else None

    @staticmethod
    def _ledger_row(r) -> dict:
        d = dict(r)
        d["unsettled"] = json.loads(d.pop("unsettled_json"))
        d["detail"] = _uj(d.pop("detail_json"))
        return d

    def write_p100_day(self, d: date | str, ledger: dict, trades: list[dict]) -> None:
        """Replace one day's P100 ledger row and trades atomically (the replay is idempotent per day)."""
        ds = _date_str(d)
        with self._lock:
            self._conn.execute("DELETE FROM p100_trades WHERE date=?", (ds,))
            self._conn.execute("DELETE FROM p100_ledger WHERE date=?", (ds,))
            self._conn.execute(
                "INSERT INTO p100_ledger(date,start_equity,end_equity,settled_cash_start,settled_cash_end,"
                "unsettled_json,trades,skipped,net_pnl,shadow_trades,shadow_net,detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ds, ledger["start_equity"], ledger["end_equity"], ledger["settled_cash_start"],
                 ledger["settled_cash_end"], json.dumps(ledger["unsettled"]), ledger["trades"], ledger["skipped"],
                 ledger["net_pnl"], ledger["shadow_trades"], ledger["shadow_net"], _j(ledger.get("detail"))))
            self._conn.executemany(
                "INSERT INTO p100_trades(date,source,symbol,signal_symbol,side,qty,entry_ts,entry_price,exit_ts,"
                "exit_price,exit_reason,gross_pnl,est_cost,net_pnl,learning_only,news_day,event,detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ds, t["source"], t["symbol"], t["signal_symbol"], t["side"], t["qty"], iso(t["entry_ts"]),
                  t["entry_price"], iso(t["exit_ts"]) if t.get("exit_ts") else None, t.get("exit_price"),
                  t.get("exit_reason"), t.get("gross_pnl"), t.get("est_cost"), t.get("net_pnl"),
                  int(bool(t.get("learning_only"))), int(bool(t.get("news_day"))), t.get("event"),
                  _j(t.get("detail"))) for t in trades])
            self._conn.commit()

    def p100_trades_for(self, d: date | str) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM p100_trades WHERE date=? ORDER BY entry_ts, id",
                                             (_date_str(d),))]

    # ---- risk days -----------------------------------------------------------
    def risk_day(self, d: date | str) -> Optional[dict]:
        r = self._one("SELECT * FROM risk_days WHERE date=?", (_date_str(d),))
        if not r:
            return None
        out = dict(r)
        out["detail"] = _uj(out.pop("detail_json"))
        out["halted"] = bool(out["halted"])
        return out

    def set_risk_open(self, d: date | str, open_equity: float, now: datetime) -> None:
        self._exec("INSERT INTO risk_days(date,open_equity,open_ts) VALUES(?,?,?) "
                   "ON CONFLICT(date) DO UPDATE SET open_equity=COALESCE(risk_days.open_equity, excluded.open_equity),"
                   " open_ts=COALESCE(risk_days.open_ts, excluded.open_ts)", (_date_str(d), open_equity, iso(now)))

    def set_risk_halt(self, d: date | str, now: datetime, detail: dict) -> None:
        self._exec("INSERT INTO risk_days(date,halted,halted_ts,detail_json) VALUES(?,1,?,?) "
                   "ON CONFLICT(date) DO UPDATE SET halted=1, halted_ts=excluded.halted_ts,"
                   " detail_json=excluded.detail_json", (_date_str(d), iso(now), json.dumps(detail)))
