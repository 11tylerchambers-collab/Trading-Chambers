"""Phase 0 → Phase 1A migration: backup first, every Phase 0 row kept as S0 / LIVE, and a recorded
Phase 0 day replays to exactly the same result through S0 after the migration. No network."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from chambers.clock import ET
from chambers.replay import build_day, replay, replay_day
from chambers.store import SCHEMA_V0, Bar, Store, iso
from chambers.strategy import Params

from .mocks import UNIVERSE

D = date(2026, 9, 24)
OPEN = datetime(2026, 9, 24, 9, 30, tzinfo=ET)
P0_PARAMS = {**Params().to_dict(), "entry_dev_pct": 0.4, "vol_mult": 1.25}


def synthetic_day() -> dict[str, list[Bar]]:
    """20 symbols, a full session, with repeated dips/rips so replay makes a few dozen trades."""
    out = {}
    for k, sym in enumerate(UNIVERSE):
        base = 50.0 + 10 * k
        bars = []
        for i in range(390):
            t = OPEN + timedelta(minutes=i)
            phase = (i + 7 * k) % 37
            c = base * (1 + (-0.006 if phase == 30 else 0.006 if phase == 15 else 0.0003 * ((i % 5) - 2)))
            v = 900.0 if phase in (15, 30) else 100.0 + (i % 7)
            bars.append(Bar(t, c, c * 1.0005, c * 0.9995, c, v))
        out[sym] = bars
    return out


def make_phase0_db(path: Path) -> dict[str, list[Bar]]:
    """A database exactly as Phase 0 writes it (schema v0, user_version 0)."""
    bars = synthetic_day()
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_V0)
    con.executemany("INSERT INTO bars(symbol,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
                    [(s, iso(b.ts), b.o, b.h, b.l, b.c, b.v) for s, bs in bars.items() for b in bs])
    con.execute("INSERT INTO cycles(ts,state,symbols_evaluated,signals_fired,orders_placed,errors,duration_ms)"
                " VALUES(?,?,?,?,?,?,?)", (iso(OPEN + timedelta(seconds=65)), "running", 20, 1, 1, 0, 812))
    con.execute("INSERT INTO signals(cycle_id,ts,symbol,close,vwap,dev_pct,vol_ratio,side,fired,reason,params_json)"
                " VALUES(1,?,?,?,?,?,?,?,1,'fired',?)",
                (iso(OPEN + timedelta(seconds=65)), "AAPL", 99.0, 100.0, -1.0, 2.0, "long", json.dumps(P0_PARAMS)))
    con.execute("INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,hypothesis_json,exit_ts,exit_price,"
                "exit_reason,bars_held,mae_pct,mfe_pct,gross_pnl,est_cost,net_pnl,params_json)"
                " VALUES('AAPL','long',20,?,99.0,'{\"expect\":\"return to vwap\"}',?,100.0,'vwap_touch',1,0,1.01,"
                "20.0,0.4,19.6,?)", (iso(OPEN + timedelta(minutes=1)), iso(OPEN + timedelta(minutes=2)),
                                    json.dumps(P0_PARAMS)))
    con.execute("INSERT INTO trades(symbol,side,qty,entry_ts,entry_price,hypothesis_json,params_json)"
                " VALUES('MSFT','short',10,?,200.0,'{}',?)", (iso(OPEN + timedelta(minutes=3)), json.dumps(P0_PARAMS)))
    con.execute("INSERT INTO heartbeat(id,state,last_cycle_ts,cycles_today,closed_today,net_pnl_today,pid)"
                " VALUES(1,'running',?,1,1,19.6,4242)", (iso(OPEN + timedelta(seconds=65)),))
    con.execute("INSERT INTO params(id,params_json,source,updated_at) VALUES(1,?,'sweep',?)",
                (json.dumps(P0_PARAMS), iso(OPEN)))
    con.execute("INSERT INTO params_history(date,params_json,source,sweep_summary_json) VALUES(?,?,?,?)",
                ("2026-09-23", json.dumps(P0_PARAMS), "sweep", json.dumps({"reason": "moved_to_best"})))
    con.execute("INSERT INTO controls(id,paused,flatten_requested,updated_at) VALUES(1,1,0,?)", (iso(OPEN),))
    con.execute("INSERT INTO errors(ts,where_,message,traceback) VALUES(?,?,?,NULL)",
                (iso(OPEN), "cycle.quotes", "boom"))
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()
    return bars


def counts(path) -> dict:
    con = sqlite3.connect(str(path))
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("bars", "cycles", "signals", "trades", "heartbeat", "params", "params_history", "controls",
                     "errors")}
    con.close()
    return out


def test_migration_backs_up_then_keeps_every_phase0_row_as_s0(tmp_path):
    db = tmp_path / "chambers.db"
    make_phase0_db(db)
    before = counts(db)
    st = Store(db)
    assert st.schema_version() == 1
    # backup taken first, with the untouched Phase 0 schema and data
    assert st.migration_backup and Path(st.migration_backup).exists()
    assert Path(st.migration_backup).parent == tmp_path / "backups"
    bk = sqlite3.connect(st.migration_backup)
    assert bk.execute("PRAGMA user_version").fetchone()[0] == 0
    assert "sleeve_id" not in [r[1] for r in bk.execute("PRAGMA table_info(trades)")]
    bk.close()
    assert counts(st.migration_backup) == before
    # nothing lost
    assert counts(db) == before
    # every row is S0 / LIVE and reads back through the Phase 0 accessors
    for table in ("cycles", "signals", "trades", "params_history"):
        rows = st._query(f"SELECT DISTINCT sleeve_id, profile FROM {table}")
        assert [tuple(r) for r in rows] == [("S0", "LIVE")], table
    assert st.read_params()["params"] == P0_PARAMS and st.read_params()["source"] == "sweep"
    assert st.read_params("S0", "LIVE") == st.read_params()
    hb = st.read_heartbeat()
    assert hb["state"] == "running" and hb["pid"] == 4242 and hb["net_pnl_today"] == 19.6
    assert st.read_heartbeat("S0") == hb
    assert [t.symbol for t in st.open_trades()] == ["MSFT"]
    closed = st.closed_trades_for_day(D)
    assert len(closed) == 1 and closed[0].net_pnl == 19.6 and closed[0].sleeve_id == "S0"
    assert closed[0].news_day is None     # Phase 0 rows carry no news tag until backfill_news_tags()
    assert st.has_sweep_for("2026-09-23") and st.read_controls()["paused"] is True
    assert st.errors_count(where="cycle.quotes") == 1
    assert len(st.signals_for_day(D)) == 1 and len(st.cycles_for_day(D)) == 1
    st.close()


def test_migration_is_one_time(tmp_path):
    db = tmp_path / "chambers.db"
    make_phase0_db(db)
    Store(db).close()
    st = Store(db)            # already v1: no second backup, no error
    assert st.migration_backup is None and st.schema_version() == 1
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_fresh_db_gets_v1_without_backup(tmp_path):
    st = Store(tmp_path / "new.db")
    assert st.schema_version() == 1 and st.migration_backup is None
    assert not (tmp_path / "backups").exists()
    cols = [r[1] for r in st._query("PRAGMA table_info(trades)")]
    assert {"sleeve_id", "profile", "news_day", "event"} <= set(cols)


def test_fresh_and_migrated_schemas_match(tmp_path):
    def schema(path):
        con = sqlite3.connect(str(path))
        out = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            out[name] = [(r[1], r[2], r[3], r[4], r[5]) for r in con.execute(f"PRAGMA table_info({name})")]
        con.close()
        return out
    Store(tmp_path / "fresh.db").close()
    make_phase0_db(tmp_path / "old.db")
    Store(tmp_path / "old.db").close()
    assert schema(tmp_path / "fresh.db") == schema(tmp_path / "old.db")


def test_phase0_day_replays_identically_as_s0_after_migration(tmp_path):
    db = tmp_path / "chambers.db"
    bars = make_phase0_db(db)
    params = Params.from_dict(P0_PARAMS)
    # what Phase 0's replay_day computes: its store returns symbols in alphabetical order
    phase0 = replay(build_day(D, dict(sorted(bars.items()))), params)
    assert len(phase0.trades) >= 10
    st = Store(db)
    # other sleeves' bars sharing the table after 1A must not leak into S0's replay
    st.write_bars("GLD", [Bar(OPEN + timedelta(minutes=i), 180.0, 180.0, 180.0, 180.0, 5000.0) for i in range(390)])
    after = replay_day(st, D, Params.from_dict(st.read_params("S0")["params"]), symbols=UNIVERSE)
    key = lambda r: [(t.symbol, t.side, t.qty, t.entry_ts, t.entry_price, t.exit_ts, t.exit_price, t.exit_reason,
                      t.bars_held, round(t.net_pnl, 10)) for t in r.trades]
    assert key(after) == key(phase0)
    assert round(after.net_pnl, 8) == round(phase0.net_pnl, 8)
    assert after.signals_evaluated == phase0.signals_evaluated
