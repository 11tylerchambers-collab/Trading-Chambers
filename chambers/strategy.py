"""VWAPReversion — the one Phase 0 strategy.

`evaluate()` is pure: it reads a SymbolState-like object and returns a Signal.
It is the single entry-decision code path for both live trading and replay.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

REASONS = ("fired", "no_position_slot", "already_in_position", "dev_too_small", "vol_too_low",
           "short_disabled", "insufficient_bars", "entries_closed")

PARAM_KEYS = ("entry_dev_pct", "vol_mult", "max_hold_bars", "stop_pct", "allow_short",
              "notional_per_trade", "max_open_positions", "min_bars_before_entry")


@dataclass(frozen=True)
class Params:
    entry_dev_pct: float = 0.30
    vol_mult: float = 1.50
    max_hold_bars: int = 10
    stop_pct: float = 0.50
    allow_short: bool = True
    notional_per_trade: float = 2000.0
    max_open_positions: int = 20
    min_bars_before_entry: int = 20

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        base = asdict(cls())
        for k in PARAM_KEYS:
            if k in d and d[k] is not None:
                base[k] = d[k]
        return cls(
            entry_dev_pct=float(base["entry_dev_pct"]),
            vol_mult=float(base["vol_mult"]),
            max_hold_bars=int(base["max_hold_bars"]),
            stop_pct=float(base["stop_pct"]),
            allow_short=_as_bool(base["allow_short"]),
            notional_per_trade=float(base["notional_per_trade"]),
            max_open_positions=int(base["max_open_positions"]),
            min_bars_before_entry=int(base["min_bars_before_entry"]),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def replace(self, **kw) -> "Params":
        d = self.to_dict()
        d.update(kw)
        return Params.from_dict(d)

    def validate(self) -> None:
        if self.entry_dev_pct <= 0 or self.vol_mult <= 0 or self.stop_pct <= 0:
            raise ValueError("entry_dev_pct, vol_mult and stop_pct must be > 0")
        if self.max_hold_bars < 1 or self.max_open_positions < 1 or self.min_bars_before_entry < 1:
            raise ValueError("max_hold_bars, max_open_positions and min_bars_before_entry must be >= 1")
        if self.notional_per_trade <= 0:
            raise ValueError("notional_per_trade must be > 0")


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass
class Signal:
    symbol: str
    side: Optional[str]          # 'long' | 'short' | None
    price: Optional[float]
    vwap: Optional[float]
    dev_pct: Optional[float]
    vol_ratio: Optional[float]
    fired: bool
    reason: str

    def to_row(self) -> dict:
        return {"symbol": self.symbol, "close": self.price, "vwap": self.vwap, "dev_pct": self.dev_pct,
                "vol_ratio": self.vol_ratio, "side": self.side, "fired": self.fired, "reason": self.reason}


def evaluate(symbol_state, params: Params, has_open_position: bool,
             slots_available: bool = True, entries_allowed: bool = True) -> Signal:
    """Entry decision for one symbol on its latest completed bar.

    Gates are checked in this order and the first failing one is the reason:
    entries_closed → insufficient_bars → already_in_position → no_position_slot →
    dev_too_small → vol_too_low → short_disabled → fired.
    Price/vwap/dev/vol are filled in whenever they can be computed, whatever the reason,
    so non-fires carry the same information as fires.
    """
    sym = symbol_state.symbol
    price = symbol_state.last_close
    vwap = symbol_state.vwap
    dev = symbol_state.dev_pct
    vol_ratio = symbol_state.vol_ratio

    def out(reason: str, side: Optional[str] = None, fired: bool = False) -> Signal:
        return Signal(sym, side, price, vwap, dev, vol_ratio, fired, reason)

    if not entries_allowed:
        return out("entries_closed")
    if symbol_state.bar_count < params.min_bars_before_entry or dev is None or vol_ratio is None:
        return out("insufficient_bars")
    if has_open_position:
        return out("already_in_position")
    if not slots_available:
        return out("no_position_slot")

    if abs(dev) < params.entry_dev_pct:
        return out("dev_too_small")
    if vol_ratio < params.vol_mult:
        return out("vol_too_low")
    if dev <= -params.entry_dev_pct:
        return out("fired", "long", True)
    # dev >= +entry_dev_pct
    if not params.allow_short:
        return out("short_disabled")
    return out("fired", "short", True)


def position_qty(price: float, params: Params) -> int:
    """qty = floor(notional / price), min 1."""
    if price <= 0:
        return 1
    return max(1, int(params.notional_per_trade // price))


def hypothesis(sig: Signal, params: Params) -> dict:
    return {"dev_pct": round(sig.dev_pct, 4) if sig.dev_pct is not None else None,
            "vol_ratio": round(sig.vol_ratio, 4) if sig.vol_ratio is not None else None,
            "expect": "return to vwap",
            "expect_move_pct": round(abs(sig.dev_pct), 4) if sig.dev_pct is not None else None,
            "expect_within_bars": params.max_hold_bars}
