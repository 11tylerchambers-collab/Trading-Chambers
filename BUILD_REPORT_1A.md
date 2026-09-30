# BUILD_REPORT_1A.md — Phase 1A build

Built 2026-09-27 on branch `phase1a` (not merged to `main`), in a Linux container, Python 3.12.3 in `.venv`.
Packages: alpaca-py 0.44.0, fastapi 0.141.1, uvicorn 0.54.0, PyYAML 6.0.3; tests: pytest 9.1.1, httpx 0.28.1.
**No new runtime dependencies**: Telegram uses `urllib`; the dashboard stays a single vanilla-JS file.

Every judgment call is in `DECISIONS.md` under "Phase 1A". The list of things I was unsure about is at the end
of this file.

**Environment limits.** This container has no Alpaca keys and no copy of the live database. So:
`--smoke`, the live `--once` per sleeve and `--broker-check` could not run against Alpaca. All three are
covered by mock-broker tests; their exact commands are in `deploy/INSTALL.md` §E.1. The migration and the
"replay a recorded Phase 0 day through S0" check were run on a database written by the **Phase 0 code
itself** (a checkout of `main`), with synthetic bars. Please repeat that check on the host with a real day
(INSTALL §E.1).

## Build-order checks (PHASE1A §13)

| # | Step | Check | Result |
|---|------|-------|--------|
| 1 | branch, backup, migration | Phase 0 day replays identically as S0 | Branch `phase1a`. `Store()` backs up with the sqlite backup API (`data/backups/pre-phase1a-*.db`), then migrates in one transaction (`user_version` 0 → 1); every Phase 0 row → S0/LIVE. Cross-version check: a DB written by `main`'s code, `--replay 2026-09-24` on `main` vs `--replay 2026-09-24 --sleeve S0` after migration → **all 383 trade lines and the summary identical** (net 4379.92). The header now also lists `skip_news_days: False` and one twin line is appended (output below). `tests/test_migration.py` — 5 passed |
| 2 | multi-cadence scheduler, per-sleeve heartbeat | S0 unchanged in `--once` | `tests/test_scheduler.py` — 4 passed: S0 inside the scheduler with an hourly UTC runner writes all 385 minute cycles; two crashing runners never cost S0 a minute; a late minute is caught up; dashboard flatten flattens every sleeve. `--once` (all sleeves started and reconciled, then one S0 cycle): 20 `entries_closed` signals after hours, as in Phase 0 (`tests/test_runtime.py`). All Phase 0 engine tests pass unchanged. |
| 3 | bar builders | tests incl. early close | `tests/test_bars.py` — 11 passed (15-min session-aligned + 13:00 early close, session-4h regular / early close / partial last bar at flatten, 30-min history input, 1-hour across midnight UTC, 5-min, session bar, indicators) |
| 4 | sizing + circuit breakers | tests | `tests/test_risk.py` — 6 passed (ATR sizing floor/min-1/cap/skip, crypto fractional, daily-loss trip once + persisted + S2 exempt, same-side cap, exposure cap, gate reason logged on S0, news-day skip) |
| 5 | S1, S2, S3 | per-sleeve tests, then `--once` each | `tests/test_sleeves.py` — 14 passed (entry/exit branches for each; S1 live entry + sizing + z-revert + cooldown; pair filter; S1 schedule 25 cycles/day, entries window, EOD flatten; S2 fractional breakout + channel exit with fees; S2 hourly across midnight UTC + 00:30 sweep; S3 decisions at 13:30:05 and flatten_at, held overnight, adopted after a restart; S1 live == replay; sweep evidence/eligibility/one-step/idempotency). `--once` each sleeve against the mock broker: `tests/test_runtime.py::test_once_each_sleeve_after_hours`. Live `--once --sleeve S1/S2/S3`: **needs keys** (INSTALL §E.1). |
| 6 | twins | reproducibility test | `tests/test_twin.py` — 7 passed: same seed → same trades, different seed → different; S0's live twin over a full day == replay from the logged seed/p, **also with a restart at noon**; bar-sleeve twin reproducible; p from trailing signals / fallback; edge = sleeve − twin |
| 7 | recon + backups | tests | `tests/test_recon.py` — 6 passed (baseline → pass within $5 → $40 mismatch alert; crypto fees; chained kinds; broker error; backup API + keep 14 + migration backup untouched; nightly chain order/waits/once; morning brief once) |
| 8 | econ calendar + news tagging | tests | `tests/test_econ.py` — 3 passed (seeded calendar valid, 6 months, sources in header; invalid file keeps previous; trades/twin trades tagged; backfill; news split) |
| 9 | Telegram | dry-run prints message text | `tests/test_notify.py` — 9 passed. Dry-run output below. |
| 10 | night lab | one full run on stored data, suggestions shown | Full grids, `--lab` on a synthetic 44-session store: 81 s, 2 suggestions (output below). `tests/test_lab.py` — 6 passed |
| 11 | P100 | one full day, ledger shown | `--p100` for 2026-09-24 and 2026-09-25 on the same store: ledger below. `tests/test_p100.py` — 5 passed (T+1: a sale's cash unusable the same day; Friday → Monday; inverse-ETF translation; shadow shorts excluded from equity; $1 minimum; carry-forward; own P100 params) |
| 12 | dashboard | render at 390 px | Headless Chromium 390×844: all six views (All, S0–S3, P100) render, `scrollWidth` 390 (no horizontal scroll); S3 param save and lab approval round-trip; two-tap flatten. Screenshots: `docs/screenshots/`. `tests/test_dashboard.py` — 11 passed (8 Phase 0 unchanged + 3 new) |
| 13 | `--broker-check`, INSTALL, report | — | `--broker-check` prints account type (from `multiplier`), `pattern_day_trader`, `daytrade_count` and any other day-trading/margin fields Alpaca returns; **needs keys**. INSTALL.md §E: deploy + migration check, BotFather steps, watchdog timer, DigitalOcean droplet backups, schedule, 1A gate. Also added `--gate-1a` (§15). |

## Test run

```
$ .venv/bin/python -m pytest -q --durations=5
192 passed, 1 warning in 25.82s
slowest: 3.47s test_gate_1a_over_a_simulated_day, 3.25s test_full_day_all_sleeves_one_process,
         3.05s test_live_twin_equals_replay_twin_and_survives_restart, 2.85s test_run_p100_..., 1.76s test_one_full_lab_run_...
```

192 tests, no network, 25.8 s (Phase 0 budget: 30 s). The 109 Phase 0 tests pass with their files unchanged
(`tests/mocks.py` gained fields for equity, multi-timeframe and crypto bars).
Per file: bars 11, broker 7, clock 7, costs 8, dashboard 11, data 6, econ 3, engine 29, gate 5, lab 6,
migration 5, notify 9, p100 5, recon 6, replay 10, risk 6, runtime 4, scheduler 4, sleeves 14, store 11,
strategy 10, sweep 8, twin 7.

## Smoke

```
$ ALPACA_PAPER=true python -m chambers.main --smoke
REFUSING TO START: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (see .env.example).      exit=2
$ ALPACA_PAPER=false python -m chambers.main --smoke
REFUSING TO START: ALPACA_PAPER must be exactly 'true' (got 'false'). Phase 0 never touches a live account.   exit=2
```

`--smoke` needs paper keys; run it on the host before starting 1A (INSTALL §E.1).

## Phase 0 day through S0 (migration check)

A database written by the Phase 0 code (`main`), 20 symbols × 390 synthetic 1-min bars on 2026-09-24:

```
$ python -m chambers.main --replay 2026-09-24                  # main, before migration
replay 2026-09-24  params={'entry_dev_pct': 0.4, 'vol_mult': 1.25, ..., 'reentry_cooldown_bars': 5}
...383 trade lines...
trades=383 evaluated=7400 fired=383 gross=4676.87 cost=296.95 net=4379.92 exits={'vwap_touch': 383}

$ python -m chambers.main --replay 2026-09-24 --sleeve S0      # phase1a, after migration
Phase 1A migration: ... backed up to .../backups/pre-phase1a-20260927-....db
replay 2026-09-24  params={..., 'reentry_cooldown_bars': 5, 'skip_news_days': False}
...383 trade lines — identical...
trades=383 evaluated=7400 fired=383 gross=4676.87 cost=296.95 net=4379.92 exits={'vwap_touch': 383}
twin (computed seed=1050559690 p=0.00000): trades=0 net=0.00  edge vs twin=4379.92

$ diff <(tail -n +2 before) <(tail -n +2 after | head -n -1) && echo IDENTICAL
IDENTICAL
```

## Messages (dry run, seeded database)

```
$ python -m chambers.main --brief evening --dry-run --date 2026-09-22
Trading Chambers - evening Tue Sep 22
S0 VWAP reversion (Phase 0): 0 trades, net +0.00, twin +0.00, edge +0.00 (20d +0.00)
S1 Index mean reversion: 1 trades, net +19.80, twin +0.00, edge +19.80 (20d +19.80)
S2 BTC breakout: 0 trades, net +0.00, twin +0.00, edge +0.00 (20d +0.00)
S3 Trend following: 0 trades, net +0.00, twin +0.00, edge +0.00 (20d +0.00)
Portfolio: net today +19.80
Recon: not run yet
Circuit breakers: none tripped
Night lab: no suggestions
$100 profile: not run
Errors today: 0

$ python -m chambers.main --brief morning --dry-run          # offline: no prices without keys
Trading Chambers - morning Sun Sep 27
Overnight:
  SPY n/a
  QQQ n/a
  BTC n/a
  GLD n/a
  USO n/a
Today: no scheduled high-impact events
S0 VWAP reversion (Phase 0): flat
S1 Index mean reversion: flat
S2 BTC breakout: flat
S3 Trend following: GLD long 12@204.50
  params: fast=10 slow=30 stop_atr=2.5
Alerts since last evening: 0
```

With prices, the overnight lines read e.g. `SPY 505.00 (+1.00%)` (tested). A fuller evening report, with
recon, circuit breakers, lab suggestions, P100 and error lines, is asserted line by line in
`tests/test_notify.py::test_evening_report_text`.

## Night lab — one full run (synthetic stored data)

44 synthetic sessions (2026-07-28 … 2026-09-25) of 1-min bars for SPY/QQQ and the 20-symbol S0
universe, 260 daily bars; full grids; `nice -n 10`. **Synthetic data: the suggestions only show the
mechanism works, not that the candidates have an edge.**

```
night lab run 1  (81.2 s, paused 0 s)
- L1_orb: sessions=44 tuned={'range_minutes': 90, 'target_r': 3.0}
    train net +3006.00 (42 tr) | graded net +4828.62 (43 tr) | twin -191.89
    SUGGEST: graded half net +4828.62 > 0 and beats its twin -191.89
- L2_pullback: sessions=44 tuned={'down_days': 3, 'max_hold': 3, 'stop_atr': 1.5}
    train net +204.94 (8 tr) | graded net +210.34 (6 tr) | twin -32.56
    SUGGEST: graded half net +210.34 > 0 and beats its twin -32.56
- L3_vwap_idx: sessions=44 tuned={'entry_dev_pct': 0.8, 'vol_mult': 1.25, 'max_hold_bars': 5, 'stop_pct': 0.3, 'skip_news_days': False}
    train net -151.93 (173 tr) | graded net -183.17 (186 tr) | twin -159.45
    no: graded half net -183.17 <= 0
- L3_vwap_news: sessions=44 tuned={'entry_dev_pct': 0.8, 'vol_mult': 1.25, 'max_hold_bars': 5, 'stop_pct': 0.3, 'skip_news_days': True}
    train net -1520.51 (1466 tr) | graded net -2210.88 (1718 tr) | twin -1320.13
    no: graded half net -2210.88 <= 0
suggestions: L1_orb, L2_pullback
```

## P100 — full days, ledger (same synthetic store)

```
$ python -m chambers.main --p100 --date 2026-09-24
P100 2026-09-24: equity 100.00 -> 99.98 (net -0.02)
  settled cash 100.00 at open -> 0.00 at close; unsettled carried: [[99.97683612, '2026-09-25']]
  matured this morning: []
  trades 1, skipped 114 {'no_settled_cash': 72, 'short_not_translatable': 42}
  shadow shorts (learning only, not in equity): 57 net -10.51
  09:54-10:04 S0           long  DIS   x0.297332 336.3240->336.7172 net -0.0232 time_stop
  10:16-10:26 S0           short AAPL  x1.085159 92.1524->92.2236 net -0.2173 time_stop [learning_only]
  ... (56 more shadow shorts)
(19 s incl. P100's own one-step tuning)

$ python -m chambers.main --p100 --date 2026-09-25
P100 2026-09-25: equity 99.98 -> 100.10 (net +0.13)
  settled cash 99.98 at open -> 0.00 at close; unsettled carried: [[100.10209407, '2026-09-28']]
  matured this morning: [[99.97683612, '2026-09-25']]
  trades 1, skipped 178 {'no_settled_cash': 32, 'short_not_translatable': 146}
  shadow shorts (learning only, not in equity): 168 net -38.12
```

Thursday's sale settles Friday; Friday's settles Monday 09-28. With $100 and T+1, P100 gets about one round
trip a day; every later signal is `no_settled_cash`. That is the point of the profile.

## What was built (files)

`store.py` (v1 schema + migration), `engine.py` (S0 + shared `SleeveBase`), `scheduler.py`, `reconcile.py`,
`bars.py`, `risk.py`, `strategies.py`, `sleeve.py`, `sleeve_replay.py`, `history.py`, `twin.py`, `runtime.py`,
`recon.py`, `jobs.py`, `econ.py` + `config/econ_calendar.yaml`, `notify.py`, `messages.py`, `watchdog.py`,
`lab.py`, `p100.py`, `gate1a.py`, dashboard (`app.py`, `static/index.html`), `deploy/chambers-watchdog.{service,timer}`,
config sections in `config.yaml`, `.env.example` Telegram keys, INSTALL §E, README header.

## Things I was unsure about (details in DECISIONS.md)

1. **The spec text was not in the message.** I used the repo file `PHASE1A_spec` (renamed to `PHASE1A_SPEC.md`) as the spec.
2. **Sweep evidence for S1–S3.** Phase 0's "≥ 20 closed trades today" and "≥ 20 trades/day" rules can't be met by sleeves that trade a few times a day. I applied them over a window (S1 20 sessions, S2 30 days, S3 120 sessions), counting replayed trades at the current params, as §2.3 does for S3. Otherwise S1–S3 would effectively never retune.
3. **S3's last-bar timing.** It decides on the 13:30–close bar at close − 5 min, using the partial bar, because an order after the close can't fill until the next open. Replay uses the full stored bar, so the two can differ by up to 5 minutes of price on that bar.
4. **Crypto fees.** I used 0.25% per side (Alpaca's lowest-tier taker rate). I don't know whether the paper account actually charges crypto fees; the S2 recon will show it (set `crypto.fee_rate: 0` if not).
5. **Econ calendar 2027.** The ten BLS/BEA dates from January to March 2027 are estimates (`confirmed: false`), because neither agency had published its 2027 schedule. Replace them when they do.
6. **P100's pool leaves out L2**, because L2 holds for several days and P100 is replayed one flat day at a time. Also, the 0.05%-per-side "slippage buffer" is my number.
7. **Lab grids.** §7 fixes L1 at 60 min / 2R and L2 at 3 days / 5 sessions / 2 ATR. Tuning needs a grid, so I put small grids around those values.
8. **Portfolio caps apply to S0 too**, since §3 says risk is shared. S0 only differs from Phase 0 when a cap binds (15 same-side positions, gross > equity, or the 2% loss halt).
9. **The morning brief can go out late.** If the engine is down at 8:45 it still sends until 3 hours after the open. The stale-heartbeat alert runs from a separate watchdog timer, which you need to enable (INSTALL §E.3).
10. **Twin frequency.** p = entries ÷ eligible evaluations over the trailing 20 signal days. Until a sleeve has history, p comes from a replay of recent stored data (p_source is logged). The twin doesn't apply cooldowns or portfolio caps.
