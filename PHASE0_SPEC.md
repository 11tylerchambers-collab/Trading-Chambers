# PHASE 0 SPEC — Heartbeat Engine
**Trading Chambers LLC · ground-zero rebuild · September 2026**

This file lives at the repo root. Build exactly what is here. Do not add features that are not in this file. If something is genuinely ambiguous, make the simplest choice that satisfies the acceptance criteria and note it in `DECISIONS.md`. Do not stop to ask unless you are truly blocked.

---

## 0. Instructions to the builder (Claude Code)

1. Read this entire file before writing any code.
2. Build in the order given in §11. Each step has a check. Run the check before moving on.
3. Write tests as you go (§12). All tests must pass before the smoke test.
4. Keep `DECISIONS.md` updated with every judgment call you make.
5. When finished, run `python -m chambers.main --smoke` and paste its output into `BUILD_REPORT.md` along with test results.
6. Do **not**: add an LLM, add a second strategy, add settings beyond §7, add a frontend build step, add a database server, add authentication beyond a single shared password, or "improve" the strategy.

---

## 1. What Phase 0 is

One deterministic strategy, running on the full Alpaca **paper** account, every market day, on its own, with an honest heartbeat and a complete log of every decision. At night it runs a mechanical parameter sweep on the day's data and adjusts its own thresholds for tomorrow.

Phase 0 is the engine with nothing on top of it. Its purpose is to prove the machine can run a full market day, every day, and produce trustworthy data. The strategy does not need to be profitable. It needs to fire often and be logged perfectly.

**Phase 0 is done when the acceptance criteria in §13 are met for five consecutive trading days.**

---

## 2. Environment and stack

- Python 3.12
- `alpaca-py` (official SDK) for trading, account, positions, orders, bars, quotes
- `fastapi` + `uvicorn` for the dashboard and control API
- `sqlite3` (stdlib) for all storage — one file, `data/chambers.db`
- `pyyaml` for config
- `zoneinfo` (stdlib) for Eastern Time. **No pytz.**
- No other runtime dependencies without a note in `DECISIONS.md`.

Runs on Linux (recommended, see §14) or Windows. No code path may depend on the OS except the launcher scripts in `deploy/`.

Secrets come from environment variables only (`.env` loaded at startup, `.env` is git-ignored):

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER=true
DASH_PASSWORD=
```

If `ALPACA_PAPER` is not exactly `true`, the process must refuse to start and print why. Phase 0 never touches a live account.

---

## 3. Repository layout

```
chambers/
├── PHASE0_SPEC.md          # this file
├── DECISIONS.md            # builder's judgment calls
├── BUILD_REPORT.md         # test + smoke output
├── README.md               # how to run, one page
├── requirements.txt
├── config.yaml             # universe + strategy params (§7)
├── .env.example
├── .gitignore              # .env, data/, __pycache__
├── chambers/
│   ├── __init__.py
│   ├── main.py             # entrypoint; --smoke, --replay, --sweep flags
│   ├── clock.py            # market calendar/clock, ET helpers
│   ├── broker.py           # alpaca-py wrapper
│   ├── data.py             # per-symbol bar state, VWAP, volume averages
│   ├── strategy.py         # VWAPReversion
│   ├── engine.py           # cycle loop, positions, exits, flatten, reconcile
│   ├── store.py            # SQLite schema and all reads/writes
│   ├── replay.py           # deterministic replay with a mock broker
│   ├── sweep.py            # nightly parameter sweep
│   └── dashboard/
│       ├── app.py          # FastAPI routes
│       └── static/
│           └── index.html  # single-file mobile-first UI, vanilla JS
├── tests/
│   ├── test_data.py
│   ├── test_strategy.py
│   ├── test_replay.py
│   ├── test_sweep.py
│   └── test_store.py
├── data/                   # runtime: chambers.db, logs/  (git-ignored)
└── deploy/
    ├── INSTALL.md
    ├── chambers.service    # systemd unit (Linux)
    └── run_windows.bat
```

---

## 4. Market clock (`clock.py`)

- All internal times are timezone-aware `datetime` in `America/New_York`. Bars from Alpaca arrive in UTC; convert immediately.
- Trading calendar and open/close times come from Alpaca's clock/calendar endpoints, cached daily. This handles holidays and early closes. Never hard-code 9:30/16:00 as the only source of truth.
- Helpers: `now_et()`, `is_market_open()`, `session_open()`, `session_close()`, `next_session_open()`, `seconds_until(dt)`.
- Phase windows within a session, derived from the calendar's close time (`C`):
  - `entries_allowed`: from open+5min until `C − 15min`
  - `flatten_at`: `C − 5min`
  - `sweep_at`: `C + 30min`

---

## 5. Broker wrapper (`broker.py`)

Thin wrapper over `alpaca-py`. Every method catches SDK exceptions, logs them via `store.log_error`, and re-raises a single `BrokerError`.

- `account()` → dict: `portfolio_value`, `buying_power`, `cash`, `equity`
- `positions()` → list of dict: `symbol`, `qty` (signed: negative = short), `avg_entry_price`, `current_price`, `unrealized_pl`
- `open_orders()` → list
- `submit_market(symbol, qty, side)` → order id. Time in force `day`.
- `order_status(order_id)` → dict: `status`, `filled_qty`, `filled_avg_price`, `filled_at`
- `cancel_all()`
- `close_all_positions()` (used by flatten as the final safety net)
- `bars_1m(symbols, start, end)` → dict symbol → list of bars `{ts, o, h, l, c, v}` in ET. One multi-symbol request. Use the IEX feed (free tier). If a symbol returns nothing, log and continue.
- `quotes(symbols)` → dict symbol → `{bid, ask, bid_size, ask_size, ts}`. One multi-symbol request.
- `clock()`, `calendar(start, end)`

Rate budget: Phase 0 must stay under ~10 API calls per cycle at 20 symbols (one bars call, one quotes call, a handful of order calls). Never loop per-symbol over the network when a multi-symbol endpoint exists.

---

## 6. Data state (`data.py`)

Per symbol, kept in memory for the current session and rebuilt from `store` on restart:

- `bars`: list of today's 1-min bars in order
- `vwap`: cumulative `Σ(typical_price × volume) / Σ(volume)` since session open, where `typical_price = (h + l + c) / 3`
- `avg_volume_20`: mean volume of the last 20 completed bars (None until 20 exist)
- `last_close`, `last_ts`

`update(symbol, new_bars)` appends only bars with `ts > last_ts` (idempotent). On startup, seed from Alpaca with all bars since session open, then persist to `store.bars`.

---

## 7. Strategy (`strategy.py`) — VWAP mean reversion

Parameters (read from `config.yaml`, overridable by the sweep):

```yaml
strategy:
  entry_dev_pct: 0.30      # |price − vwap| / vwap × 100 must exceed this
  vol_mult: 1.50           # bar volume must be ≥ vol_mult × avg_volume_20
  max_hold_bars: 10        # time stop
  stop_pct: 0.50           # adverse move from entry, in %, → exit
  allow_short: true
  notional_per_trade: 2000 # dollars; qty = floor(notional / price), min 1
  max_open_positions: 20
  min_bars_before_entry: 20   # need avg_volume_20
universe:
  - AAPL
  - MSFT
  - NVDA
  - AMZN
  - GOOGL
  - META
  - TSLA
  - AMD
  - NFLX
  - JPM
  - BAC
  - XOM
  - SPY
  - QQQ
  - COST
  - WMT
  - DIS
  - INTC
  - CRM
  - AVGO
```

`evaluate(symbol_state, params, has_open_position) -> Signal`

Signal fields: `symbol, side ('long'|'short'|None), price, vwap, dev_pct, vol_ratio, fired (bool), reason` where `reason` is one of:
`fired`, `no_position_slot`, `already_in_position`, `dev_too_small`, `vol_too_low`, `short_disabled`, `insufficient_bars`, `entries_closed`.

Entry logic:
- `dev_pct = (close − vwap) / vwap × 100`
- If `dev_pct <= −entry_dev_pct` and `vol_ratio >= vol_mult` → `long`
- If `dev_pct >= +entry_dev_pct` and `vol_ratio >= vol_mult` and `allow_short` → `short`
- Otherwise not fired, with the first failing reason in the order above.

**Every evaluation is logged, fired or not.** The nightly sweep depends on the non-fires.

Exit logic (evaluated every bar for each open position, before entries):
1. `vwap_touch`: long and `close >= vwap`, or short and `close <= vwap`
2. `stop_loss`: adverse move from entry ≥ `stop_pct`
3. `time_stop`: `bars_held >= max_hold_bars`
4. `eod_flatten`: at `flatten_at` regardless

Each open position tracks `mae_pct` and `mfe_pct` (max adverse / max favorable excursion from entry, updated every bar). These are logged at exit and are the raw material for tuning stops later.

The hypothesis written at entry:
```json
{"dev_pct": -0.41, "vol_ratio": 1.9, "expect": "return to vwap", "expect_move_pct": 0.41, "expect_within_bars": 10}
```

---

## 8. Engine loop (`engine.py`)

State machine: `idle → preopen → running → flattening → postclose → sweeping → idle`. State is written to `store.heartbeat` every transition and every cycle. There is no separate "running" boolean. The dashboard derives health from `last_cycle_ts` only.

Cycle runs at `hh:mm:05` ET for every minute from `session_open` to `flatten_at`:

1. Read `controls` (pause flag) from store.
2. `bars = broker.bars_1m(universe, last_ts+1m, now)` → `data.update`
3. `quotes = broker.quotes(universe)` (used for cost estimate and logging)
4. For each open position: evaluate exits; on exit submit market order, record `exit_reason`, wait for fill (poll `order_status` up to 5s), write trade close.
5. If not paused and `entries_allowed`: for each symbol evaluate entry; log the signal; if fired and slots available, submit market order, poll for fill, write trade open with hypothesis.
6. Write `cycles` row and `heartbeat`.
7. Sleep until next `hh:mm:05`.

Rules:
- One open position per symbol.
- `est_cost` for a live trade = `|fill − quote mid|` at entry + `|fill − quote mid|` at exit, per share, × qty, plus `0.01% × notional × 2` slippage assumption. Log the raw bid/ask at both ends. `net_pnl` for a live trade = `gross_pnl − 0.01% × notional × 2`: live `gross_pnl` comes from real fills, which already carry the spread, so the fill-vs-mid part of `est_cost` is recorded as a diagnostic and not subtracted again. *(Amended 2026-09-23. The original rule, half the quoted spread at each end subtracted from a fill-based gross, counted the spread twice and used IEX quotes that are often far wider than the market; see DECISIONS.md.)*
- Any exception inside a cycle is caught, logged to `errors`, counted in `cycles.errors`, and the loop continues. **The process must never die from a strategy or broker error.** Only a fatal startup condition (bad keys, `ALPACA_PAPER != true`) exits.
- If a bars fetch fails, skip evaluations that cycle but still write the `cycles` row with `errors ≥ 1`.

**Flatten** at `flatten_at`: submit market exits for all open trades, wait, then call `broker.close_all_positions()` as a safety net, then `cancel_all()`. Log any position that had to be closed by the safety net as `exit_reason = eod_safety_net`.

**Reconcile** on every startup and at `preopen`:
- Pull `broker.positions()` and `store.open_trades()`.
- Matching symbol and sign → adopt, continue managing.
- Position in broker with no open trade in store → orphan: flatten immediately, log to `errors` with `where = reconcile`.
- Open trade in store with no broker position → mark closed with `exit_reason = reconcile_missing`, `exit_price` = last known close.

**Pause** (from dashboard) stops new entries only. Exits keep running. **Flatten now** (from dashboard) runs the flatten routine immediately.

---

## 9. Storage (`store.py`)

SQLite, WAL mode, single writer (the engine process). Dashboard reads only, except `controls` and `params` writes.

```sql
cycles(id, ts, state, symbols_evaluated, signals_fired, orders_placed, errors, duration_ms)
signals(id, cycle_id, ts, symbol, close, vwap, dev_pct, vol_ratio, side, fired, reason, params_json)
trades(id, symbol, side, qty,
       entry_ts, entry_price, entry_order_id, entry_bid, entry_ask, hypothesis_json,
       exit_ts, exit_price, exit_order_id, exit_bid, exit_ask, exit_reason,
       bars_held, mae_pct, mfe_pct, gross_pnl, est_cost, net_pnl, params_json)
bars(symbol, ts, o, h, l, c, v)              -- PRIMARY KEY (symbol, ts)
heartbeat(id=1, state, last_cycle_ts, cycles_today, signals_today, fired_today,
          opened_today, closed_today, open_positions, net_pnl_today,
          last_error, last_error_ts, pid, started_at)
params(id=1, params_json, source, updated_at)   -- current live params
params_history(id, date, params_json, source, sweep_summary_json)
controls(id=1, paused, flatten_requested, updated_at)
errors(id, ts, where_, message, traceback)
```

All timestamps stored as ISO-8601 strings with offset. Provide typed read/write functions; no raw SQL outside `store.py`.

---

## 10. Nightly sweep (`sweep.py`) and replay (`replay.py`)

**Replay** runs the exact `strategy.py` and the exact exit logic from `engine.py` over stored `bars` with a mock broker that fills at the bar close and charges `est_cost` using a fixed spread assumption of 0.02% (no quotes in replay); because replay fills are at the close, not a real fill, replay keeps `est_cost = half spread at each end × qty + slippage` and `net_pnl = gross_pnl − est_cost`. Replay and live must share the same strategy and exit code paths — no duplicated logic. `python -m chambers.main --replay 2026-09-22` prints the trade list and summary using the live params; `--params entry_dev_pct=0.30,vol_mult=1.50` overrides any of them for that replay only.

**Sweep** runs at `sweep_at`:

1. Load the last N session dates that exist in `bars` (N = 5, or fewer if fewer exist).
2. Grid:
   - `entry_dev_pct ∈ {0.20, 0.30, 0.40, 0.50, 0.60, 0.80}`
   - `vol_mult ∈ {1.00, 1.25, 1.50, 2.00}`
   - `max_hold_bars ∈ {5, 10, 15, 20}`
   - `stop_pct ∈ {0.30, 0.50, 0.75}`
3. Replay every combination across all N days. Score = **total net PnL**, but a combination is ineligible if it produced fewer than 20 trades per day on average.
4. Choose the best eligible combination. Then apply the **one-step rule**: each parameter may move at most one grid step from the current value per night. If the best is more than one step away, move one step in its direction.
5. Do not change params if today produced fewer than 20 closed trades (insufficient evidence).
6. Write the chosen params to `params` (source = `sweep`) and a full summary (all combos, scores, trade counts, chosen, reason) to `params_history`.
7. Engine reads `params` at `preopen` the next day.

The sweep must finish in under 10 minutes on a 2-core machine. Vectorize the hot loop if needed, but keep the strategy logic shared.

---

## 11. Build order with checks

1. `store.py` + schema → test: create db, write/read each table.
2. `clock.py` → test: ET conversion, `entries_allowed` windows against a mocked calendar with an early close.
3. `broker.py` → smoke: `--smoke` prints account, 3 bars for AAPL, a quote for SPY.
4. `data.py` → test: VWAP against a hand-computed 5-bar example; idempotent `update`.
5. `strategy.py` → test: each `reason` reachable; long/short fire correctly; `allow_short=false` path.
6. `replay.py` → test: a synthetic day with a known dip-and-recover produces exactly one long trade with `exit_reason = vwap_touch`; a synthetic drift produces a `stop_loss`; a flat day produces a `time_stop`.
7. `engine.py` → run one live cycle after hours with `--once`: writes a `cycles` row and 20 `signals` rows with `reason = entries_closed`.
8. `sweep.py` → test: on a synthetic 2-day dataset, the one-step rule is enforced and `params_history` is written.
9. `dashboard/` → open on a phone-width viewport; all sections render from a seeded db.
10. `deploy/` → service file installs and restarts on kill.
11. Reconcile test: seed an orphan position in a mock broker; engine flattens it and logs.

---

## 12. Tests

`pytest`. No network in tests; mock the broker. Minimum coverage: every branch in `strategy.py` and the exit logic, VWAP math, sweep one-step rule, reconcile cases. Tests must run in under 30 seconds.

---

## 13. Acceptance criteria — the Phase 0 gate

Five consecutive trading days, all of the following, verified from the database, not the UI:

1. `cycles` has a row for ≥ 98% of minutes between `session_open` and `flatten_at` each day.
2. ≥ 30 closed trades per day.
3. Every closed trade has non-null `hypothesis_json`, `exit_reason`, `mae_pct`, `mfe_pct`, `est_cost`, `net_pnl`.
4. `signals` row count per day ≈ minutes × 20 (every evaluation logged).
5. Sweep ran each night and wrote `params_history`.
6. Zero rows in `errors` with `where_ = 'unhandled'` (all errors were caught in-cycle).
7. Heartbeat honesty: at every spot check during market hours, `last_cycle_ts` was within 90 seconds of now.
8. Restart test passed at least once mid-session: process killed, supervisor restarted it within 60 seconds, reconcile ran, no orphans remained, trading resumed.

Provide `python -m chambers.main --gate` which checks 1–6 over the last 5 sessions and prints PASS/FAIL per item.

---

## 14. Deployment and mobile access

**Recommended: a small Linux VPS** (2 vCPU, 2 GB, Ubuntu 24.04). The engine must run during market hours every day; a desktop that sleeps is the single biggest cause of "it was green all day but nothing happened." `deploy/chambers.service` runs the engine as a systemd service with `Restart=always`, `RestartSec=10`. `deploy/INSTALL.md` covers: clone, venv, `.env`, service install, log location (`journalctl -u chambers`).

**Fallback: Windows desktop.** `deploy/run_windows.bat` runs the engine; `INSTALL.md` includes the `powercfg` commands to disable sleep and a Task Scheduler entry that starts the bat at logon. Note the risk plainly.

**Phone access:** install Tailscale on the host and the phone. The dashboard binds to `0.0.0.0:8080` and is reached at the host's Tailscale IP. No public port, no port forwarding, no TLS setup needed. `INSTALL.md` includes the three Tailscale steps. The dashboard password is still required.

**Editing from the phone:** the repo lives on GitHub. Config changes (thresholds, universe, pause, flatten) are done in the dashboard. Code changes are done through Claude Code against the host (remote session from the Claude mobile app, or SSH via the Tailscale IP). The engine reads `params` from the database, so a config change never requires a restart; a code change does — `INSTALL.md` says how.

---

## 15. Dashboard (`dashboard/`)

Single HTML file, mobile-first (designed at 390 px wide, works wider), vanilla JS polling `/api/state` every 5 seconds. Password prompt on first load; token kept in memory. No framework, no build step.

Sections top to bottom:

1. **Heartbeat bar** — state badge; "last cycle Ns ago" (green < 90 s during market hours, red otherwise, grey when market closed); market open/closed; next open countdown.
2. **Today** — cycles, signals, fired, opened, closed, open positions, net PnL today, portfolio value, buying power.
3. **Controls** — Pause entries / Resume; Flatten now (two-tap confirm).
4. **Open positions** — symbol, side, qty, entry, current, unrealized, bars held, exit trigger distance.
5. **Recent trades** — last 25 closed: time, symbol, side, net PnL, exit reason, bars held. Tap a row to see the hypothesis and MAE/MFE.
6. **Params** — current values with source and date; editable form; Save applies next cycle and writes `params_history` with `source = manual`.
7. **Sweep history** — last 7 nights: date, chosen params, delta from prior, trades, net PnL, reason if unchanged.
8. **Errors** — last 10.

API:
```
GET  /api/state         -> everything the page needs, one call
POST /api/params        -> {params}
POST /api/pause         -> {paused: bool}
POST /api/flatten
POST /api/login         -> {password} -> {token}
```

---

## 16. What is explicitly out of scope for Phase 0

LLM of any kind · second strategy · Mimir/meta-learning/Osiris · Obsidian · Firebase · strategy toggles · account-mode switching · backtesting beyond `--replay` · notifications · charts · React.

Anything here goes in Phase 1 or later, after the gate in §13 is passed.
