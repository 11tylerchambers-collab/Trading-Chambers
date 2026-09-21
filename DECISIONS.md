# DECISIONS.md — builder's judgment calls

Each entry: what was ambiguous or unstated, what I chose, and why. Ordered by build step.

## Environment

- **Python 3.12 via venv.** The system default here is 3.11; `/usr/bin/python3.12` exists, so the build and all tests run in a `.venv` created with 3.12 to match §2. Code uses nothing newer than 3.11 features anyway.
- **Runtime dependencies are exactly** `alpaca-py`, `fastapi`, `uvicorn`, `pyyaml`. `pytest` and `httpx` are test-only (httpx is what FastAPI's `TestClient` needs). `tzdata` is listed for Windows only because `zoneinfo` has no system tz database there. `.env` is parsed by a 15-line stdlib loader in `main.py` rather than adding `python-dotenv`.
- **Existing root `index.html`** from the original upload is left untouched. It is not part of the §3 layout, but deleting a user file is not a build step. The dashboard lives at `chambers/dashboard/static/index.html` as specified.

## Step 1 — store.py

- **One `Store` class**, one connection, an `RLock`, `check_same_thread=False`. The dashboard runs as a thread in the engine process (see engine notes) and gets its own `Store` instance on the same file; WAL plus a 30s busy timeout handles the two connections.
- **Day queries use `substr(ts,1,10)`.** All timestamps are written through `iso()`, which converts to Eastern Time before serializing, so the date prefix is always the ET session date.
- **`trades.bars_held / mae_pct / mfe_pct` are updated every cycle** for open trades (`update_trade_progress`), not only at exit. This is what lets a restarted engine adopt a position with its excursion history intact instead of starting MAE/MFE from zero.
- **`heartbeat.write_heartbeat` is a partial upsert**: only the fields passed change. This keeps `last_cycle_ts` honest — it is only ever set by a completed cycle, never by idle-state pings.
- **Schema additions beyond §9:** indexes on `ts`/`exit_ts`, and `CHECK (id = 1)` on the singleton tables. No extra columns.
