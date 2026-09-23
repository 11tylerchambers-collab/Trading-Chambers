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

## Step 5 — strategy.py

- **Reason precedence.** The spec lists the reasons but "first failing reason in the order above" cannot be read literally (a `dev_too_small` check needs a VWAP, which needs bars). Checks run in the only order that makes each reason reachable: `entries_closed` → `insufficient_bars` → `already_in_position` → `no_position_slot` → `dev_too_small` → `vol_too_low` → `short_disabled` → `fired`. The three entry conditions keep the spec's order (deviation, then volume, then the short switch).
- **Non-fires carry the numbers.** `close`, `vwap`, `dev_pct`, `vol_ratio` are filled whenever they can be computed regardless of the reason, including `entries_closed`. The sweep and any later analysis need them.
- **Thresholds are inclusive** (`<=`, `>=`), as written in §7.
- **`paused` logs as `entries_closed`.** The reason enum is fixed by the spec and has no `paused` value; a pause is "entries not allowed right now", which is what `entries_closed` says. Exits still run while paused.
- **Every symbol is evaluated and logged every cycle**, including after hours and while paused. §8 step 5 reads as if evaluation only happens inside the entry window, but §11.7 expects 20 `entries_closed` signals from an after-hours cycle and §13.4 expects one signal per symbol per minute. Orders are only placed when the window is open and not paused.

## Steps 6–7 — engine.py / replay.py

- **Shared code path.** `Position` (bars_held, MAE/MFE), `check_exit` and `trade_economics` live in `engine.py`; `replay.py` imports them and `strategy.evaluate`. The replay loop mirrors `Engine.run_cycle` (exits, then entries, then eod flatten) but with a `ReplayBroker` that fills at the bar close. It does not drive the full `Engine` class because the engine's per-cycle SQLite writes (7,700 signal rows/day) would make the 288-combo sweep take hours instead of seconds.
- **MAE/MFE are signed percentages from the bar close**: `mae_pct <= 0` is the worst signed move, `mfe_pct >= 0` the best. Computed from closes, not highs/lows, so they are on the same basis as the exit checks (which also use the close). The exit price is included as the final observation.
- **`bars_held` counts completed bars after the entry bar.** The bar that fired the signal is bar 0; the next new bar makes it 1. `time_stop` fires when `bars_held >= max_hold_bars`.
- **Cycle bars window.** At `hh:mm:05` the fetch ends at `hh:mm:00 − 1s`, so the minute still forming is never evaluated. The start is the most-lagging symbol's `last_ts + 1min`.
- **Stale bars.** If IEX has no new bar for a symbol, the previous bar is evaluated again. This cannot produce a duplicate entry (the position guard) and cannot change an exit decision, so it is tolerated rather than adding a reason outside the spec's enum.
- **Re-entry cooldown (added at the owner's request after the initial build; see the section below).** Previously a symbol could be re-entered on the same bar that stopped it out.
- **Unfilled orders after 5s** are recorded at the last bar close (entry) or last close (exit), and an error is logged. A market order on a liquid paper account fills within the first poll in practice; if one does not, reconcile at the next preopen and the eod safety net correct the books.
- **`est_cost` slippage** uses the entry notional (`qty × entry_price`) for both legs.
- **Manual flatten** writes `exit_reason = manual_flatten` so it is not confused with `eod_flatten`. It does not pause entries; pause first if re-entry is not wanted.
- **Flatten safety net.** `broker.positions()` is read before `close_all_positions()`; whatever was still held is logged with `eod_safety_net` and, if a store trade exists for it, that trade is closed with the position's current price.
- **Reconcile adoption rebuilds `bars_held`/MAE/MFE** from the bars stored since the entry minute, merged with the values persisted each cycle, so a restart mid-trade does not reset the time stop.
- **`errors.where_` values**: `cycle.controls`, `cycle.bars`, `cycle.quotes`, `cycle.exit`, `cycle.entry`, `cycle.write`, `flatten.*`, `reconcile`, `params`, `sweep`, `startup.seed_bars`, `preopen.seed_bars`, `broker.<method>`, and `unhandled` only for an exception that escapes `tick()` itself — which is exactly what §13.6 forbids.
- **Heartbeat during waits**: the idle/preopen/postclose waits rewrite `state` every 30s but never `last_cycle_ts`; only a completed cycle sets it.
- **Preopen starts at open − 15 min.** The spec names the phase but not its start; 15 minutes is enough for reconcile and bar seeding without leaving the process idle for long.
- **Params are re-read from the store every cycle**, so both the nightly sweep result and a dashboard edit apply on the next cycle, which also satisfies "engine reads params at preopen".
- **`Engine(sweep_fn=...)`** is an injection point for tests only; the default is `sweep.run_sweep`.

## Step 8 — sweep.py

- **Speed.** Bars are reduced once per day to parameter-independent `Snapshot`s (close, VWAP, vol_ratio, bar_count); each of the 288 combinations then replays over those. A full 5-day × 20-symbol grid takes ~14 s single-threaded here, so no vectorization or multiprocessing was added.
- **`params` is written only when the chosen values differ from current.** Rule 5 ("do not change params") and an unchanged best would otherwise overwrite a `manual` source with `sweep`. `params_history` is written every night regardless, with the full results list (288 entries: params, net_pnl, trades, trades_per_day, eligible), best, chosen, reason and today's closed-trade count.
- **Non-grid current values** (e.g. a manual `entry_dev_pct: 0.33`) are snapped to the nearest grid point before the one-step rule is applied.
- **Session times for replay days** come from the calendar when the engine runs the sweep (`sessions=` argument); the CLI `--replay`/`--sweep` use the calendar if credentials are present and fall back to 9:30–16:00 otherwise.
- **`--sweep` on the CLI** runs the same `run_sweep` immediately, for a manual re-run or a first look; it is not a schedule.

## Step 9 — dashboard

- **Runs in-process.** `python -m chambers.main` starts uvicorn in a daemon thread of the engine process, so one systemd unit covers both and the dashboard dies and restarts with the engine. It opens its own `Store` connection; SQLite WAL handles the two connections in one process.
- **Portfolio value and buying power** are not in the heartbeat schema (§9 is fixed), so the dashboard calls `broker.account()` itself, cached for 30 s, which is one Alpaca call per 30 s regardless of how many phones are polling. With no broker (offline test) the tiles show "—".
- **"Exit trigger distance"** is shown as three numbers per position: `to vwap` (percent move still needed to touch VWAP), `to stop` (percent of adverse room left before `stop_pct`, red under 0.15%), and `bars held / max`. Current price and VWAP are rebuilt from the position's session bars in the store.
- **Sweep history "trades / net PnL"** are the chosen combination's replay results over the sweep window (that is what the sweep scored), shown next to the live closed-trade count that gated the change. The delta is against the previous sweep row.
- **Manual params save writes `params_history` with `source = manual` and no summary**, and `params` with `source = manual`. Unknown keys, non-numeric values and out-of-range values are rejected with a 400 before anything is written.
- **Tokens** are random 32-byte strings held in a process-memory set: a restart logs every browser out, which is the intended behavior of "token kept in memory". Password comparison is constant-time. An empty `DASH_PASSWORD` makes login fail with a clear 503 instead of allowing an empty password.
- **No favicon route**: the browser's `/favicon.ico` request 404s harmlessly. Not worth a route or an inline icon.
- **Verified** with headless Chromium at 390×844 against a seeded database: all eight sections render, login rejects a wrong password, pause/resume, two-tap flatten and params save all round-trip to the database, and the page has no horizontal scroll (tables scroll inside their cards).

## Step 10 — deploy

- **systemd unit** runs as a dedicated `chambers` user from `/opt/chambers` with `Restart=always`, `RestartSec=10`; the unit could not be installed in the build container (no systemd), so the kill/restart check is written up as a command sequence in `INSTALL.md` §A.5 for the host.
- **Windows** gets a `.bat` restart loop (the equivalent of `Restart=always`) plus `powercfg` and Task Scheduler instructions; `INSTALL.md` states the sleep/update risk up front.
- **Stop behavior**: SIGTERM ends the process without flattening. Open positions are adopted by reconcile on the next start; the eod flatten and safety net still run at close if the process is back. Flattening on stop was not specified and would make a routine `systemctl restart` close trades.

## --gate

- **Criterion 4 ("≈ minutes × 20")** is implemented as ≥ 98% of `minutes × universe_size`, counting only signals timestamped inside `[open, flatten_at)`, so after-hours `--once` runs neither help nor hurt. 98% matches criterion 1.
- **Criterion 1** counts distinct cycle minutes inside the window, so a duplicate cycle in one minute cannot pad the ratio.
- **Sessions come from the calendar when keys are present** (early closes shrink the minute denominator); without keys the gate assumes 9:30–16:00.
- The gate reports the two manual criteria (7, 8) as a reminder line and exits non-zero unless all six items pass **and** five sessions exist.

## Fill polling

- `wait_fill` polls `timeout / 0.25` times (20 polls for 5 s) instead of comparing wall-clock time, so the 5-second budget is exact in production and instant under a fake clock in tests.

## Re-entry cooldown (owner request, after the initial build)

- **What.** New param `reentry_cooldown_bars` (default **5**) in `config.yaml`, the `params` table and the dashboard form. After any exit on a symbol, entries on that symbol are blocked until that many bars have completed since the exit bar. `0` turns it off; `1` blocks only same-bar re-entry.
- **Why it exists.** Without it, a `stop_loss` or `time_stop` exit could be followed by a new entry in the same direction on the same bar, because the deviation that caused the stop still satisfies the entry rule. That churned trades on a thesis that had just failed.
- **Departures from the spec, accepted by the owner.** §7 lists a fixed set of settings and a fixed reason enum, and §0 says not to change the strategy. This adds one setting and one reason, `cooldown`. It is checked after `already_in_position` and before `no_position_slot`, so every blocked evaluation is still logged with its price, VWAP, deviation and volume ratio.
- **Shared code path.** The check lives in `strategy.evaluate` (a `bars_since_exit` argument, computed with `strategy.bars_since`). The engine and replay each record the symbol's bar count at exit, so live trading and the sweep behave identically.
- **Restart safe.** On startup and at preopen the engine rebuilds each symbol's exit bar count from today's closed trades and stored bars. A restart mid-cooldown does not reopen the door early. The cooldown resets each session.
- **All exit reasons count**, including `vwap_touch` (harmless: at VWAP the entry rule cannot fire anyway), `manual_flatten` and `eod_safety_net`. `reconcile_missing` does not, because no bar-level exit happened.
- **Not in the sweep grid.** The §10 grid is unchanged; the sweep carries the live cooldown value through every combination.
- **Effect on the gate.** A cooldown lowers trade count. If §13.2 (≥ 30 closed trades/day) is hard to reach, lower it from the dashboard; no restart needed.

## Live trade costing (owner request, 2026-09-23)

- **What was wrong.** §8 set live `est_cost` to half the quoted bid/ask spread at each end plus 0.01% slippage per side, and `net_pnl = gross_pnl − est_cost`. Live `gross_pnl` is computed from real broker fills, which already include whatever the spread cost, so the spread was counted twice. The quotes also come from the IEX feed, whose spread is often far wider than the whole market (on 2026-09-23 the median was 8.9 bps, the mean 59 bps and the worst 1,007 bps; CRM's median was about 500 bps).
- **Effect on 2026-09-23 (171 trades).** Live `net_pnl` was −$1,912.31 against an actual paper-account equity change of +$37.62. Almost all of the difference was the half-spread term ($1,887.51).
- **What changed.** Live trades now use `engine.live_trade_economics`. `est_cost = (|entry fill − mid| + |exit fill − mid|) × qty + 0.01% × notional × 2` is stored as a diagnostic of execution quality. `net_pnl = gross_pnl − 0.01% × notional × 2`: only the slippage assumption is subtracted, because the fill-vs-mid part is already inside `gross_pnl`. The owner chose this over subtracting the full `est_cost`, which would still have counted the fill-vs-mid amount twice. With `|·|`, a fill better than mid still adds to `est_cost`; that is intended for a diagnostic, and it does not affect `net_pnl`.
- **Missing quotes.** A leg with no quote, or a crossed quote, contributes 0 to the fill-vs-mid term. Closes that are not real fills (`reconcile_missing`, `eod_safety_net`) have no exit quote, so their exit leg is slippage only.
- **Replay and the sweep are unchanged.** Replay fills at the bar close, which is roughly the mid, so its half-spread cost is still an honest estimate and `net_pnl = gross_pnl − est_cost` still applies there. Replay keeps using `trade_economics`, so live and replay no longer share the cost function. They still share the strategy and exit code.
- **Backfill.** `python -m chambers.main --recompute-costs` re-costs every closed trade from its stored fills and quotes, and is safe to run more than once. It was run once on 2026-09-23 after the close.
- **Known limitation.** IEX mids are still used for the diagnostic `est_cost`, so that number is noisy for symbols with stale or wide IEX quotes. Replacing or filtering the quote source is deferred to Phase 1A.

## --replay --params (owner request, 2026-09-23)

- `--replay` uses the current `params` row, which the nightly sweep may already have moved by the time you replay the same day. `--params k=v,k=v` overrides any key in `PARAM_KEYS` for that replay only; unspecified keys keep their live values. Unknown keys or entries without `=` are rejected before the database is opened. `--params` without `--replay` is an error. Nothing is written.
