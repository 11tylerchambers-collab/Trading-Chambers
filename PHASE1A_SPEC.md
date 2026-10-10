# PHASE 1A SPEC — Sleeves, Risk, Night Lab, Alerts
**Trading Chambers LLC · builds on Phase 0 · September 2026**

Build exactly what is here. Judgment calls go in `DECISIONS.md`. Do not add anything from §14.

---

## 0. Instructions to the builder (Claude Code)

1. Read this file and `PHASE0_SPEC.md` + `DECISIONS.md` before writing code. Phase 0 behavior stays intact unless this file changes it.
2. **Build on branch `phase1a`. Do not merge to `main` or restart the service until the user confirms the Phase 0 gate has passed (`--gate` all PASS over 5 sessions).**
3. Build in the order in §13, running each check before moving on. Write tests as you go (no network in tests).
4. Migrate the existing database in place. Existing Phase 0 rows become sleeve `S0`. Back up the db before migrating. Never lose Phase 0 data.
5. When done: full test suite, `--smoke`, a replay of one recorded Phase 0 day through `S0` that reproduces its Phase 0 result, and `BUILD_REPORT_1A.md`.

---

## 1. What Phase 1A is

The engine goes from one strategy to several **sleeves**. A sleeve = one strategy on one asset group, with its own virtual capital, its own params, its own nightly sweep, its own random twin, its own history. **Nothing is averaged across sleeves.** Only portfolio risk is shared.

It also adds: volatility-based 1% risk sizing, portfolio circuit breakers, a daily broker reconciliation check, phone alerts and daily messages, a suggest-only night lab, a nightly $100 cash-account replay profile, and a news-day filter.

Still paper only. Still refuses to start unless `ALPACA_PAPER=true`.

---

## 2. Sleeves

| ID | Strategy | Assets | Bars | Holds | Direction |
|----|----------|--------|------|-------|-----------|
| S0 | VWAP reversion (Phase 0, unchanged) | Phase 0 universe | 1-min | intraday | long + short |
| S1 | Index mean reversion | SPY, QQQ | 15-min | intraday | long + short |
| S2 | Breakout | BTC/USD | 1-hour | multi-day, 24/7 | long only (Alpaca crypto is spot) |
| S3 | Trend following | GLD, USO | session 4-hour (§2.3) | multi-day, overnight | long + short |

Each sleeve has a virtual capital balance (`sleeves.capital`), default **$20,000** each, configurable. Sleeve P&L accrues to its own balance. No reallocation in 1A (that's 1B).

### 2.1 S1 — SPY/QQQ 15-min mean reversion
- 15-min bars built from 1-min bars, session-aligned (first bar 9:30–9:45).
- `z = (close − SMA20) / stdev20` on 15-min closes.
- Entry: `z ≤ −entry_z` → long; `z ≥ +entry_z` → short. Default `entry_z = 2.0`.
- Exit, first of: `z` crosses 0; stop at `stop_atr × ATR14(15m)` from entry (default 2.0); time stop `max_hold_bars` (default 8); EOD flatten.
- **Pair filter:** SPY and QQQ are one risk bucket. If one is open in a direction, the other may not open in the same direction.
- Sweep grid: `entry_z ∈ {1.5, 2.0, 2.5, 3.0}`, `stop_atr ∈ {1.5, 2.0, 3.0}`, `max_hold_bars ∈ {4, 8, 12}`.

### 2.2 S2 — BTC/USD 1-hour breakout
- Runs **24/7** on its own schedule, independent of the equity session. Cycle at `hh:00:10` UTC on 1-hour bars.
- Entry (long only): close > highest high of prior `lookback` bars (default 24) and bar volume ≥ `vol_mult` × 24-bar average (default 1.5).
- Exit, first of: close < lowest low of prior `exit_lookback` bars (default 12); stop at `stop_atr × ATR14(1h)` (default 2.0).
- Fractional quantity allowed. Use Alpaca's actual crypto fee schedule in cost estimates; document the rate used in `DECISIONS.md`.
- Sweep runs daily at 00:30 UTC. Grid: `lookback ∈ {12, 24, 48}`, `vol_mult ∈ {1.0, 1.5, 2.0}`, `exit_lookback ∈ {6, 12, 24}`, `stop_atr ∈ {1.5, 2.0, 3.0}`.

### 2.3 S3 — GLD/USO trend following
- "Session 4-hour" bars: two bars per session, 9:30–13:30 and 13:30–close (handles early closes).
- Long when `EMA_fast > EMA_slow` and close > `EMA_fast`; short on the mirror condition. Defaults `fast=10`, `slow=30` (in session-4h bars).
- Exit on the opposite cross or stop at `stop_atr × ATR14` (default 2.5).
- **Holds overnight.** Exempt from EOD flatten. Reconcile must handle overnight positions.
- Slow sleeve: sweep runs only if ≥ 20 closed trades exist in its lookback window (use up to 120 sessions of history, fetched from Alpaca on first run and stored). Otherwise record `insufficient_evidence`.
- Grid: `fast ∈ {5, 10, 20}`, `slow ∈ {20, 30, 50}` (fast < slow only), `stop_atr ∈ {2.0, 2.5, 3.0}`.

### 2.4 Shared rules for all sleeves
- One-step rule and 20-trade evidence rule from Phase 0 apply per sleeve (S3 uses its window as in §2.3).
- Sweeps are idempotent per sleeve per day (same fix as Phase 0).
- Re-entry cooldown per sleeve, param `reentry_cooldown_bars` (S0 keeps 5; others default 2).
- One open position per symbol per sleeve. If two sleeves want the same symbol, both may hold it; positions are tracked per sleeve and broker quantity is the sum. Reconcile per sleeve.

---

## 3. Risk

### 3.1 Position sizing — 1% risk (S1, S2, S3)
`qty = (sleeve_capital × risk_pct) / stop_distance`, where `stop_distance` is the dollar distance from entry to the ATR-based stop. Default `risk_pct = 0.01`. Cap notional at `max_notional_pct` of sleeve capital (default 50%). Whole shares for equities (floor, min 1, skip if 1 share exceeds the cap), fractional for BTC.
S0 keeps its Phase 0 fixed-notional sizing.

### 3.2 Portfolio circuit breakers
- **Daily loss limit:** if total realized + unrealized P&L across sleeves since the session open falls below `−daily_loss_pct` of account equity (default 2%), halt new entries in all equity sleeves for the rest of the session. Exits continue. Alert (§6).
- **Same-side cap:** at most `max_same_side` open positions in the same direction across all sleeves (default 15).
- **No leverage:** total gross exposure ≤ account equity. Entries that would exceed it are skipped with reason `exposure_cap`.

---

## 4. Random twins

Every sleeve gets a twin: same symbols, same bar timeframe, same exits, same sizing, but **random entries** at the same average frequency as the real sleeve over its trailing 20 sessions, with random direction where the sleeve allows both. Twins are **simulated only** (no orders), evaluated on live bars each cycle, costs included, stored in `twin_trades`. Seed the RNG per sleeve per day and log the seed so any twin day is reproducible.

Scoreboard metric shown everywhere: **sleeve net P&L − twin net P&L**, per day and trailing 20 sessions.

---

## 5. Data and accounting

### 5.1 Feed
Config `data_feed: iex | sip` (default `iex`). SIP is used when the user enables a paid Alpaca data plan. No code change needed to switch.

### 5.2 Daily broker reconciliation
After each equity session and after the S2 daily rollover, compare:
`Δ Alpaca equity` vs `Σ realized gross P&L of all sleeves + Δ unrealized P&L of open positions − fees`.
Store in `recon` table. If `|difference| > max($5, 0.01% of equity)`, alert (§6) with the numbers.

### 5.3 Backups
Nightly SQLite backup (`.backup` API, not file copy) to `data/backups/chambers-YYYY-MM-DD.db`, keep 14. Document enabling DigitalOcean droplet backups in `INSTALL.md` (user action, not code).

### 5.4 News-day filter
- `config/econ_calendar.yaml`: dated high-impact US events (FOMC decisions, CPI, jobs report, PCE, GDP advance). Seed it for the next 6 months from official release calendars and document the sources. The user can edit it.
- Every trade and twin trade gets `news_day: bool` and `event` (nullable).
- Per-sleeve param `skip_news_days` (default `false`). Reports show results split by news vs non-news days.

### 5.5 Settlement/PDT note
Add a `--broker-check` command that reports the account type, whether Alpaca currently flags pattern day trading, and the day-trade count, so we know how Alpaca has implemented the 2026 FINRA rule change. Report only; no behavior depends on it in 1A.

---

## 6. Messages and alerts (Telegram)

`.env` gains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. If missing, messaging is disabled with one startup warning; nothing else breaks. `INSTALL.md` includes the BotFather steps to get both.

**Morning brief** (8:45 ET market days): overnight change for SPY, QQQ, BTC, GLD, USO; today's econ calendar events; each sleeve's open positions and params; any alerts since last evening.

**Evening report** (after the last equity sleeve sweep): per sleeve — trades, net P&L, twin net P&L, edge vs twin (day and trailing 20), param changes; portfolio totals; recon result; circuit breakers tripped; night lab suggestions (§7); $100 profile result (§8); errors count.

**Immediate alerts:** heartbeat stale > 3 min during any sleeve's active hours; daily loss limit tripped; recon mismatch; engine restart during market hours; any `unhandled` error. Rate-limit identical alerts to one per 15 min.

Keep messages short and readable on a phone. Plain text, no tables.

---

## 7. Night lab (suggest only, never trades)

Runs after the evening report on market days, as a **separate process at lower CPU priority** (`nice 10`). It must never block or slow the engine; if the engine's cycle duration exceeds 20 s while the lab runs, the lab pauses.

Candidate strategies, replayed on stored bars with the same cost model and a random twin each:
- **L1 Opening range breakout** on SPY/QQQ, 5-min bars: range = 9:30–10:30 high/low; enter on first 5-min close outside the range after 10:30; stop at the opposite side of the range or 1× range height, whichever is closer; target 2R; EOD flatten.
- **L2 Pullback in uptrend** on SPY/QQQ: long when price > 200-session SMA and close has fallen 3 sessions in a row (session bars); exit on first up close or after 5 sessions; stop 2× ATR.
- **L3 VWAP variants**: S0's strategy on S1's symbols, and S0 with `skip_news_days=true`.

Scoreboard per candidate over a rolling window (up to 60 sessions; fetch history from Alpaca on first run and store).

**Suggestion rule** — a candidate is suggested only if, over ≥ 20 sessions:
1. Params are tuned on the first half of the window and graded on the second half (unseen);
2. Graded-half net P&L > 0 after costs; and
3. Graded-half net P&L beats its twin.

Suggestions appear in the evening report and a dashboard panel with the numbers behind them. **Nothing is added to live trading without the user's explicit approval** (a dashboard button that records the approval; wiring an approved candidate into a live sleeve is a separate build request).

---

## 8. $100 cash-account profile (nightly replay)

A replay-only profile `P100` that trades the day's stored data as a **$100 cash account**:
- Long only. Bearish exposure only via inverse ETFs **SH** (S&P 500) and **PSQ** (Nasdaq); add both to the stored-bars universe.
- Cash settles next business day (T+1): proceeds from a sale are unusable until settled. Carry the settlement ledger across days.
- Fractional shares, minimum order $1. Same cost model plus slippage buffer.
- 1% risk per trade on current P100 equity.
- Strategy pool: S0 and S1 logic (translated to long-only; a short signal on SPY becomes a long SH signal, QQQ → PSQ), plus any lab candidate currently meeting §7's rule. P100 tunes and scores its own params, stored under profile `P100`, fully separate from live sleeves.
- **Shadow shorts:** also record the untranslated short signals as simulated shorts, tagged `learning_only`. Report them, never count them in P100's equity or readiness.
- Equity carries forward day to day from $100. Report daily P100 equity and trailing results in the evening message.

---

## 9. Storage changes

Add `sleeve_id` (and `profile` where relevant, default `LIVE`) to `cycles`, `signals`, `trades`, `params`, `params_history`. Existing rows → `S0`/`LIVE`.

New tables: `sleeves` (id, name, strategy, symbols, timeframe, capital, active), `twin_trades`, `recon`, `alerts`, `econ_events`, `lab_runs`, `lab_results`, `lab_suggestions` (with approval status), `p100_ledger`, `p100_trades`, `bars_1h`, `bars_session` (or a generic bars table keyed by timeframe — builder's choice, documented).

All SQL stays in `store.py`. Keep WAL and the single-writer rule; the lab and P100 replay write only their own tables.

---

## 10. Engine changes

- Scheduler handles multiple cadences: 1-min (S0), 15-min (S1), hourly 24/7 (S2), session-4h (S3). One process; each sleeve's cycle is independent and an exception in one never affects the others.
- Heartbeat becomes per sleeve plus an overall row. The dashboard shows each.
- EOD flatten applies to S0 and S1 only. S2 and S3 carry positions.
- Reconcile at startup and preopen, per sleeve, including overnight and crypto positions. Orphans → flatten + alert.
- Partial-fill handling from the Phase 0 fix applies to every sleeve.

---

## 11. Dashboard

Keep the Phase 0 page. Add a sleeve selector at the top (All, S0–S3, P100). Per sleeve: heartbeat, today's stats, **edge vs twin** (today, trailing 20), open positions, recent trades, params (editable), sweep history. Portfolio card: equity, daily P&L vs loss limit, exposure, same-side count, last recon result. Night lab panel with suggestions and approve/reject buttons. Alerts panel (last 20).

---

## 12. Tests

Everything from Phase 0 still passes. Add: each sleeve's entry/exit branches; session-4h and 15-min bar building with an early close; 24/7 scheduling across midnight UTC; ATR sizing and caps; pair filter; daily loss limit; same-side and exposure caps; twin reproducibility from seed; recon pass/fail; T+1 settlement in P100 (a sale's cash unusable same day); inverse-ETF translation; shadow shorts excluded from P100 equity; lab train/grade split; suggestion rule; Telegram disabled cleanly when unconfigured; migration keeps Phase 0 rows and reproduces a Phase 0 day.

---

## 13. Build order with checks

1. Branch `phase1a`, back up db, migration → Phase 0 day replays identically as `S0`.
2. Multi-cadence scheduler + per-sleeve heartbeat → S0 unchanged in `--once`.
3. Bar builders (15-min, session-4h, 1h) → tests incl. early close.
4. Sizing + circuit breakers → tests.
5. S1, then S2, then S3 → per-sleeve tests, then `--once` each.
6. Twins → reproducibility test.
7. Recon + backups → tests.
8. Econ calendar + news tagging → tests.
9. Telegram (morning, evening, alerts) → dry-run prints message text.
10. Night lab (separate process, `nice`) → one full run on stored data, suggestion output shown.
11. P100 replay → one full day, ledger shown.
12. Dashboard additions → render at 390 px.
13. `--broker-check`, INSTALL.md updates, `BUILD_REPORT_1A.md`.

---

## 14. Out of scope for 1A

Auto-allocation between sleeves and walk-forward grading of live sleeves (1B) · strategy mutation/invention and per-sleeve AI critique (1C) · wiring approved lab candidates into live sleeves automatically · options · real money · leverage.

---

## 15. Phase 1A gate

Ten consecutive trading days, verified from the database by `--gate-1a`:
1. Every active sleeve wrote cycles for ≥ 98% of its expected cycles (S2 around the clock).
2. Each active sleeve wrote a signal/evaluation row with a reason for ≥ 98% of its expected cycles. Closed trades per sleeve per day are reported, not graded. *(Changed 2026-10-10 from "S0 ≥ 30 closed trades/day; S1 ≥ 1/day average"; see DECISIONS.md.)*
3. Every closed trade and twin trade has complete fields (hypothesis, exit_reason, costs, news_day).
4. Recon passed every day, or every mismatch was explained and fixed.
5. Sweeps, night lab, and P100 replay ran every night.
6. Morning and evening messages delivered on ≥ 95% of days; a test alert was received.
7. Zero `unhandled` errors; at least one mid-session restart recovered cleanly, including an overnight S3 position.
