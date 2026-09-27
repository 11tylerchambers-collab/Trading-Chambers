"""Phase 1A strategies S1–S3 (PHASE1A §2). Pure functions on lists of completed bars.

Each strategy is a `StrategySpec`: its sleeve, bar timeframe, symbols, params (defaults, types,
sweep grid) and two functions shared by live trading (`sleeve.BarSleeve`) and replay
(`sleeve_replay`):

  entry(hist, params) -> EntryEval    the strategy's view of the latest bar: side or a reason
  exit(pos, hist, params) -> reason   the first exit condition that holds on the latest bar, or None

`evaluate_entry()` wraps `entry` with the gates every sleeve shares, in this order (the first failing
one is the reason, as in Phase 0): entries_closed → insufficient_bars → already_in_position →
cooldown → the strategy's own reasons → fired. Numbers are filled in whatever the reason.

Stops are fixed at entry (entry ∓ stop_atr × ATR14) and, like Phase 0's stop, checked on bar closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .bars import TF_15M, TF_1H, TF_S4H, atr, avg_volume, ema_series, highest_high, lowest_low, sma, stdev
from .store import Bar

COMMON_DEFAULTS = {"risk_pct": 0.01, "max_notional_pct": 0.50, "reentry_cooldown_bars": 2, "skip_news_days": False}
COMMON_TYPES = {"risk_pct": float, "max_notional_pct": float, "reentry_cooldown_bars": int, "skip_news_days": bool}


@dataclass
class EntryEval:
    side: Optional[str]              # 'long' | 'short' | None
    reason: str
    price: Optional[float]
    stop_distance: Optional[float] = None
    detail: dict = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.side is not None and self.reason == "fired"


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass(frozen=True)
class StrategySpec:
    sleeve_id: str
    name: str
    strategy: str
    timeframe: str
    symbols: tuple
    allow_short: bool
    eod_flatten: bool                # S1: flattened at flatten_at; S2, S3 carry
    fractional: bool                 # S2 crypto
    defaults: dict
    types: dict
    grid: dict
    entry: Callable
    exit: Callable
    min_bars: int
    expect: str
    hist_bars: int = 100             # bars of history the strategy sees, live and in replay alike
    grid_ok: Callable[[dict], bool] = lambda p: True
    pair_filter: bool = False        # S1: SPY and QQQ are one risk bucket

    # ---- params ------------------------------------------------------------
    def params(self, d: Optional[dict] = None) -> dict:
        out = dict(self.defaults)
        for k, v in (d or {}).items():
            if k in self.types and v is not None:
                out[k] = v
        for k, t in self.types.items():
            out[k] = _as_bool(out[k]) if t is bool else t(out[k])
        return out

    def validate(self, p: dict) -> None:
        for k, t in self.types.items():
            if t in (int, float) and k != "reentry_cooldown_bars" and p[k] <= 0:
                raise ValueError(f"{k} must be > 0")
        if p["reentry_cooldown_bars"] < 0:
            raise ValueError("reentry_cooldown_bars must be >= 0")
        if p["risk_pct"] > 0.05 or p["max_notional_pct"] > 1.0:
            raise ValueError("risk_pct must be <= 0.05 and max_notional_pct <= 1.0")
        if not self.grid_ok(p):
            raise ValueError("params outside the strategy's constraints (e.g. fast < slow)")

    def grid_combos(self, base: dict) -> list[dict]:
        import itertools
        keys = list(self.grid)
        out = []
        for vals in itertools.product(*(self.grid[k] for k in keys)):
            p = {**base, **dict(zip(keys, vals))}
            if self.grid_ok(p):
                out.append(p)
        return out


# ==========================================================================
# S1 — SPY/QQQ 15-minute mean reversion
# ==========================================================================

def s1_z(hist: Sequence[Bar]) -> Optional[tuple[float, float, float]]:
    closes = [b.c for b in hist]
    m, sd = sma(closes, 20), stdev(closes, 20)
    if m is None or sd is None:
        return None
    return ((closes[-1] - m) / sd if sd > 0 else 0.0), m, sd


def s1_entry(hist: Sequence[Bar], p: dict) -> EntryEval:
    price = hist[-1].c if hist else None
    zz = s1_z(hist) if hist else None
    a = atr(hist, 14) if hist else None
    if zz is None or a is None:
        return EntryEval(None, "insufficient_bars", price)
    z, m, sd = zz
    detail = {"z": round(z, 4), "sma20": round(m, 4), "stdev20": round(sd, 4), "atr14": round(a, 4)}
    stop = p["stop_atr"] * a
    if z <= -p["entry_z"]:
        return EntryEval("long", "fired", price, stop, detail)
    if z >= p["entry_z"]:
        return EntryEval("short", "fired", price, stop, detail)
    return EntryEval(None, "z_too_small", price, stop, detail)


def s1_exit(pos, hist: Sequence[Bar], p: dict) -> Optional[str]:
    c = hist[-1].c
    zz = s1_z(hist)
    if zz is not None:
        z = zz[0]
        if (pos.side == "long" and z >= 0) or (pos.side == "short" and z <= 0):
            return "z_revert"
    if pos.stop_price is not None:
        if (pos.side == "long" and c <= pos.stop_price) or (pos.side == "short" and c >= pos.stop_price):
            return "stop_loss"
    if pos.bars_held >= p["max_hold_bars"]:
        return "time_stop"
    return None


S1 = StrategySpec(
    sleeve_id="S1", name="Index mean reversion", strategy="zscore_reversion_15m", timeframe=TF_15M,
    symbols=("SPY", "QQQ"), allow_short=True, eod_flatten=True, fractional=False,
    defaults={"entry_z": 2.0, "stop_atr": 2.0, "max_hold_bars": 8, **COMMON_DEFAULTS},
    types={"entry_z": float, "stop_atr": float, "max_hold_bars": int, **COMMON_TYPES},
    grid={"entry_z": [1.5, 2.0, 2.5, 3.0], "stop_atr": [1.5, 2.0, 3.0], "max_hold_bars": [4, 8, 12]},
    entry=s1_entry, exit=s1_exit, min_bars=21, expect="z returns to 0 (price back to SMA20)", pair_filter=True,
    hist_bars=60)


# ==========================================================================
# S2 — BTC/USD 1-hour breakout (long only)
# ==========================================================================

VOL_AVG_BARS = 24


def s2_entry(hist: Sequence[Bar], p: dict) -> EntryEval:
    price = hist[-1].c if hist else None
    if not hist:
        return EntryEval(None, "insufficient_bars", price)
    hh = highest_high(hist, p["lookback"])
    av = avg_volume(hist, VOL_AVG_BARS)
    a = atr(hist, 14)
    if hh is None or av is None or a is None:
        return EntryEval(None, "insufficient_bars", price)
    vr = hist[-1].v / av if av > 0 else 0.0
    detail = {"breakout_level": round(hh, 4), "vol_ratio": round(vr, 4), "avg_vol24": round(av, 6),
              "atr14": round(a, 4)}
    stop = p["stop_atr"] * a
    if price <= hh:
        return EntryEval(None, "no_breakout", price, stop, detail)
    if vr < p["vol_mult"]:
        return EntryEval(None, "vol_too_low", price, stop, detail)
    return EntryEval("long", "fired", price, stop, detail)


def s2_exit(pos, hist: Sequence[Bar], p: dict) -> Optional[str]:
    c = hist[-1].c
    ll = lowest_low(hist, p["exit_lookback"])
    if ll is not None and c < ll:
        return "channel_exit"
    if pos.stop_price is not None and c <= pos.stop_price:
        return "stop_loss"
    return None


S2 = StrategySpec(
    sleeve_id="S2", name="BTC breakout", strategy="breakout_1h", timeframe=TF_1H,
    symbols=("BTC/USD",), allow_short=False, eod_flatten=False, fractional=True,
    defaults={"lookback": 24, "vol_mult": 1.5, "exit_lookback": 12, "stop_atr": 2.0, **COMMON_DEFAULTS},
    types={"lookback": int, "vol_mult": float, "exit_lookback": int, "stop_atr": float, **COMMON_TYPES},
    grid={"lookback": [12, 24, 48], "vol_mult": [1.0, 1.5, 2.0], "exit_lookback": [6, 12, 24],
          "stop_atr": [1.5, 2.0, 3.0]},
    entry=s2_entry, exit=s2_exit, min_bars=49, expect="breakout continues until the channel low breaks",
    hist_bars=100)


# ==========================================================================
# S3 — GLD/USO session-4h trend following (holds overnight)
# ==========================================================================

def s3_emas(hist: Sequence[Bar], p: dict) -> Optional[tuple[float, float]]:
    closes = [b.c for b in hist]
    f, s = ema_series(closes, p["fast"]), ema_series(closes, p["slow"])
    if not closes or f[-1] is None or s[-1] is None:
        return None
    return f[-1], s[-1]


def s3_entry(hist: Sequence[Bar], p: dict) -> EntryEval:
    price = hist[-1].c if hist else None
    e = s3_emas(hist, p) if hist else None
    a = atr(hist, 14) if hist else None
    if e is None or a is None:
        return EntryEval(None, "insufficient_bars", price)
    ef, es = e
    detail = {"ema_fast": round(ef, 4), "ema_slow": round(es, 4), "atr14": round(a, 4)}
    stop = p["stop_atr"] * a
    if ef > es and price > ef:
        return EntryEval("long", "fired", price, stop, detail)
    if ef < es and price < ef:
        return EntryEval("short", "fired", price, stop, detail)
    return EntryEval(None, "no_trend", price, stop, detail)


def s3_exit(pos, hist: Sequence[Bar], p: dict) -> Optional[str]:
    c = hist[-1].c
    e = s3_emas(hist, p)
    if e is not None:
        ef, es = e
        if (pos.side == "long" and ef < es) or (pos.side == "short" and ef > es):
            return "trend_reversal"
    if pos.stop_price is not None:
        if (pos.side == "long" and c <= pos.stop_price) or (pos.side == "short" and c >= pos.stop_price):
            return "stop_loss"
    return None


S3 = StrategySpec(
    sleeve_id="S3", name="Trend following", strategy="ema_trend_s4h", timeframe=TF_S4H,
    symbols=("GLD", "USO"), allow_short=True, eod_flatten=False, fractional=False,
    defaults={"fast": 10, "slow": 30, "stop_atr": 2.5, **COMMON_DEFAULTS},
    types={"fast": int, "slow": int, "stop_atr": float, **COMMON_TYPES},
    grid={"fast": [5, 10, 20], "slow": [20, 30, 50], "stop_atr": [2.0, 2.5, 3.0]},
    entry=s3_entry, exit=s3_exit, min_bars=51, expect="trend persists until the EMAs cross back",
    grid_ok=lambda p: p["fast"] < p["slow"], hist_bars=250)   # EMAs are computed over exactly these bars

SPECS = {"S1": S1, "S2": S2, "S3": S3}


# ==========================================================================
# shared gate wrapper
# ==========================================================================

def evaluate_entry(spec: StrategySpec, hist: Sequence[Bar], p: dict, has_position: bool,
                   bars_since_exit: Optional[int], entries_allowed: bool) -> EntryEval:
    ev = spec.entry(hist, p)
    if ev.fired and ev.side == "short" and not spec.allow_short:
        ev = EntryEval(None, "short_disabled", ev.price, ev.stop_distance, ev.detail)

    def out(reason: str) -> EntryEval:
        return EntryEval(None, reason, ev.price, ev.stop_distance, ev.detail)

    if not entries_allowed:
        return out("entries_closed")
    if ev.reason == "insufficient_bars":
        return ev
    if has_position:
        return out("already_in_position")
    if bars_since_exit is not None and bars_since_exit < p["reentry_cooldown_bars"]:
        return out("cooldown")
    return ev


def hypothesis(spec: StrategySpec, ev: EntryEval, p: dict, stop_price: float, entry_bar_ts: str) -> dict:
    h = {"strategy": spec.strategy, "side": ev.side, "expect": spec.expect, "stop_price": round(stop_price, 6),
         "stop_distance": round(ev.stop_distance, 6), "entry_bar_ts": entry_bar_ts, **ev.detail}
    if "max_hold_bars" in p:
        h["expect_within_bars"] = p["max_hold_bars"]
    return h


def stop_price_for(side: str, price: float, stop_distance: float) -> float:
    return price - stop_distance if side == "long" else price + stop_distance
