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

## Step 3 — broker.py / main.py

- **`--smoke` could not be run in the build environment.** Alpaca's API is reachable (the endpoint answers 401 with no credentials) but no keys exist here. The wrapper is instead covered by `tests/test_broker.py`, which swaps the two SDK clients for fakes and checks the ET conversion, signed short quantities, the single multi-symbol request, the IEX feed flag, and the error → `store.errors` → `BrokerError` path. The smoke test must be run once on the host with real paper keys.
- **`Broker(paper=False)` raises**, in addition to the `ALPACA_PAPER` check in `main.py`. Two guards, one intent.
- **Missing bars for a symbol are logged at DEBUG**, not to `errors`. IEX volume is thin and a quiet minute for one symbol is routine, not an error; writing it to `errors` would drown the dashboard's error list.
- **Alpaca calendar times** come out of alpaca-py as naive `datetime`s built from the date plus "HH:MM" wall time; they are ET, so they are tagged `America/New_York` directly rather than converted from UTC.
- **`.env` never overrides a variable already set in the environment** (`setdefault`), so systemd `Environment=` and shell exports win.
- **Logging**: stdout (for journalctl) plus a 10 MB rotating file at `data/logs/chambers.log`. The `--smoke/--replay/--gate` commands log to stdout only.

## Step 4 — data.py

- **`avg_volume_20` includes the latest bar.** "Last 20 completed bars": the bar the cycle evaluates is completed, so it is one of the 20. This is also what makes `min_bars_before_entry: 20` sufficient; excluding the current bar would require 21.
- **`DataState.last_ts()` is the minimum across symbols**, so the next bars fetch starts from the most lagging symbol and `update()`'s `ts > last_ts` filter discards the duplicates for the others.
- **Zero-volume bars** are appended (they count toward bar_count and the volume window) but contribute nothing to VWAP; VWAP is `None` until any volume has traded.
