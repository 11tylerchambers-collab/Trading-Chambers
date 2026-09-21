# Trading Chambers — Phase 0: Heartbeat Engine

One deterministic strategy (VWAP mean reversion) on a full Alpaca **paper**
account, every market day, on its own, with an honest heartbeat and a complete
log of every decision. Each night it sweeps a fixed parameter grid over the
last five days of bars and moves its thresholds at most one step.

The spec is `PHASE0_SPEC.md`. Judgment calls are in `DECISIONS.md`. Test and
smoke output is in `BUILD_REPORT.md`. Host setup is in `deploy/INSTALL.md`.

## Run

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER=true, DASH_PASSWORD
.venv/bin/python -m chambers.main --smoke      # account, 3 AAPL bars, SPY quote
.venv/bin/python -m chambers.main              # engine + dashboard on http://0.0.0.0:8080
```

`ALPACA_PAPER` must be exactly `true` or the process refuses to start.

| Command | What it does |
|---|---|
| `python -m chambers.main` | The service: engine loop plus dashboard, forever |
| `--smoke` | Prints account, market clock, 3 AAPL bars, a SPY quote |
| `--once` | Runs one cycle now and exits (after hours: 20 `entries_closed` signals) |
| `--replay YYYY-MM-DD` | Replays stored bars for that day with the live params; prints trades and summary |
| `--sweep` | Runs the nightly grid sweep now |
| `--gate` | Phase 0 acceptance checks 1–6 over the last 5 sessions, PASS/FAIL per item |

Tests: `.venv/bin/python -m pytest` (no network; the broker is mocked).

## How a day goes

| ET | State | What happens |
|---|---|---|
| open − 15 min | `preopen` | Load params from the DB, seed today's bars, reconcile positions against the broker |
| open → close − 5 min | `running` | Every minute at `:05`: fetch bars (one call), quotes (one call), evaluate exits, evaluate and log an entry signal for every symbol, write `cycles` + `heartbeat` |
| close − 5 min | `flattening` | Market-out every open trade, `close_all_positions()` safety net, `cancel_all()` |
| → close + 30 min | `postclose` | Wait |
| close + 30 min | `sweeping` | 288-combination grid over the last 5 days; one-step rule; write `params` + `params_history` |
| otherwise | `idle` | Wait for the next session |

Entries are allowed from open + 5 min until close − 15 min. Session times come
from Alpaca's calendar, so early closes and holidays are handled.

## Layout

```
chambers/main.py       entrypoint and CLI flags
chambers/clock.py      calendar-aware ET clock and phase windows
chambers/broker.py     alpaca-py wrapper; every error → store.errors + BrokerError
chambers/data.py       per-symbol bars, VWAP, 20-bar average volume
chambers/strategy.py   VWAPReversion: evaluate() → Signal with a reason, always logged
chambers/engine.py     cycle loop, positions, exits, flatten, reconcile, state machine
chambers/store.py      SQLite schema and every read/write (no SQL elsewhere)
chambers/replay.py     deterministic replay sharing the strategy and exit code
chambers/sweep.py      nightly grid sweep with the one-step rule
chambers/gate.py       --gate acceptance checks
chambers/dashboard/    FastAPI API + single-file mobile dashboard
data/                  chambers.db and logs/ (git-ignored)
deploy/                systemd unit, Windows launcher, INSTALL.md
```

## Dashboard

Mobile-first single HTML file at `/`, polling `/api/state` every 5 s. One
shared password (`DASH_PASSWORD`), token kept in memory. Sections: heartbeat,
today's counters, controls (pause entries / flatten now with two-tap
confirm), open positions, last 25 trades (tap for hypothesis and MAE/MFE),
editable params, sweep history, last 10 errors. Reach it over Tailscale; see
`deploy/INSTALL.md`.

## Phase 0 gate

Phase 0 is done when `--gate` passes for five consecutive trading days and
the two manual checks (heartbeat spot checks during market hours, one
mid-session kill/restart with clean reconcile) have been done. Nothing from
spec §16 is built until then.
