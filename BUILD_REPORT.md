# BUILD_REPORT.md — Phase 0 build

Built on 2026-09-21 in a Linux container, Python 3.12.3 in `.venv`.
Packages: alpaca-py 0.44.0, fastapi 0.141.1, uvicorn 0.53.0, PyYAML 6.0.1; tests: pytest 9.1.1, httpx 0.28.1.

## Build-order checks (spec §11)

| # | Step | Check | Result |
|---|------|-------|--------|
| 1 | store.py | create db, write/read each table | `tests/test_store.py` — 10 passed |
| 2 | clock.py | ET conversion, entry windows against a mocked calendar with a 13:00 early close | `tests/test_clock.py` — 7 passed |
| 3 | broker.py | `--smoke` prints account, 3 AAPL bars, SPY quote | **SKIPPED — no Alpaca keys in the build environment** (the API is reachable, it answers 401 without credentials). Wrapper covered by `tests/test_broker.py` with the SDK clients faked — 7 passed. Run `python -m chambers.main --smoke` on the host; see below for what it prints. |
| 4 | data.py | VWAP vs hand-computed 5-bar example; idempotent update | `tests/test_data.py` — 6 passed |
| 5 | strategy.py | every reason reachable; long/short; `allow_short=false` | `tests/test_strategy.py` — 10 passed |
| 6 | replay.py | dip-and-recover → one long `vwap_touch`; drift → `stop_loss`; flat → `time_stop` | `tests/test_replay.py` — 10 passed |
| 7 | engine.py | one cycle after hours writes a `cycles` row and 20 `signals` with `entries_closed` | Done against the mock broker (`test_once_after_hours_writes_cycle_and_20_entries_closed_signals`) — the live `--once` needs keys, same as step 3. `tests/test_engine.py` — 21 passed |
| 8 | sweep.py | 2-day synthetic dataset: one-step rule enforced, `params_history` written | `tests/test_sweep.py` — 7 passed. Full 288-combo grid over 5 synthetic days of 20 symbols: 13.7 s single-threaded (budget: 10 min on 2 cores) |
| 9 | dashboard/ | phone-width viewport, all sections render from a seeded db | Headless Chromium at 390×844: all 8 sections rendered; wrong password rejected; pause/resume, two-tap flatten and params save round-trip to the db; page scrollWidth 390 (no horizontal scroll). `tests/test_dashboard.py` — 7 passed |
| 10 | deploy/ | service installs and restarts on kill | **Not runnable here (no systemd in the container).** Unit has `Restart=always`, `RestartSec=10`; the kill/restart procedure is in `deploy/INSTALL.md` §A.5 |
| 11 | reconcile | orphan in mock broker is flattened and logged | `test_reconcile_orphan_is_flattened_and_logged` + missing/adopt/sign-mismatch cases — passed |

`--gate`: `tests/test_gate.py` — 4 passed (five good days PASS; each item fails independently; early-close denominator; empty db).

## Test run

```
$ .venv/bin/python -m pytest -q --durations=3
........................................................................ [ 80%]
.................                                                        [100%]
=============================== warnings summary ===============================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  /home/user/Trading-Chambers/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
============================= slowest 3 durations ==============================
1.72s call     tests/test_gate.py::test_gate_uses_last_five_sessions_and_early_close
1.57s call     tests/test_sweep.py::test_sweep_on_two_synthetic_days_enforces_one_step_and_writes_history
1.13s call     tests/test_gate.py::test_gate_fails_each_item_independently
89 passed, 1 warning in 8.46s
```

89 tests, no network, 8.46 s (budget: 30 s).

## Smoke test

```
$ .venv/bin/python -m chambers.main --smoke
SKIPPED: no ALPACA_API_KEY / ALPACA_SECRET_KEY available in the build environment.
```

The startup guards were exercised instead:

```
$ ALPACA_PAPER=false .venv/bin/python -m chambers.main --smoke
REFUSING TO START: ALPACA_PAPER must be exactly 'true' (got 'false'). Phase 0 never touches a live account.
exit=2
$ ALPACA_PAPER=true .venv/bin/python -m chambers.main --smoke
REFUSING TO START: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (see .env.example).
exit=2
```

On the host with paper keys, `--smoke` prints: `== account ==` (portfolio_value, buying_power, cash, equity),
`== clock ==`, `== 3 bars for AAPL (<date>) ==` (last 3 one-minute bars of the most recent session, ET),
`== quote for SPY ==` (bid/ask/sizes/ts). Paste that output here after the first run.

## Offline CLI checks (seeded synthetic database)

```
$ python -m chambers.main --replay 2026-09-18
replay 2026-09-18  params={'entry_dev_pct': 0.3, 'vol_mult': 1.5, 'max_hold_bars': 10, 'stop_pct': 0.5, ...}
entry    exit     sym    side    qty         in        out bars    mae%    mfe%     gross    cost       net  reason
10:01:05 10:02:05 AAPL   long     20    99.4000   100.0000    1   0.000   0.604     12.00    0.80     11.20  vwap_touch
...
trades=460 evaluated=7400 fired=460 gross=5394.70 cost=358.03 net=5036.67 exits={'vwap_touch': 460}

$ python -m chambers.main --sweep
sweep 2026-09-21: insufficient_evidence: 0 closed trades today (< 20); params unchanged
  days=['2026-09-16', '2026-09-17', '2026-09-18'] combos=288 eligible=192
  best={'entry_dev_pct': 0.2, 'vol_mult': 1.0, 'max_hold_bars': 5, 'stop_pct': 0.3} score=15110.0
  (5.2 s for 3 days x 20 symbols x 288 combos)

$ python -m chambers.main --gate
Phase 0 gate over sessions: (none)
[FAIL] 1..6 — no sessions in database
NOTE: only 0 session(s) in the database; the gate needs 5 consecutive.
OVERALL: FAIL
```

The gate fails correctly on a database with no live cycles; the PASS path is covered by `tests/test_gate.py`.

## Still to do on the host (cannot be done in the build environment)

1. `--smoke` with real paper keys, paste output above.
2. `--once` after hours: expect one `cycles` row and 20 `signals` rows with `reason = entries_closed`.
3. Install `deploy/chambers.service`, `systemctl kill -s SIGKILL chambers`, confirm restart within 10 s and `reconcile:` lines in the journal (§13.8).
4. Five consecutive trading days, then `--gate`.

## Re-entry cooldown (added after the initial build)

`reentry_cooldown_bars` (default 5) blocks re-entering a symbol for that many bars after an exit. It is covered by `test_reentry_cooldown` (strategy), `test_reentry_cooldown_after_stop_loss` (replay) and `test_cooldown_blocks_same_bar_reentry_and_survives_restart` (engine, including restart recovery). Suite total: 89 passed.
