"""Per-symbol intraday bar state: cumulative VWAP, 20-bar average volume.

Kept in memory for the current session; rebuilt from `store.bars` on restart.
`update()` is idempotent: only bars with ts > last_ts are appended.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .store import Bar

VOL_WINDOW = 20


class SymbolState:
    __slots__ = ("symbol", "bars", "_pv", "_v")

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bars: list[Bar] = []
        self._pv = 0.0  # Σ typical_price × volume
        self._v = 0.0   # Σ volume

    # ---- derived values --------------------------------------------------
    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def last_ts(self) -> Optional[datetime]:
        return self.bars[-1].ts if self.bars else None

    @property
    def last_close(self) -> Optional[float]:
        return self.bars[-1].c if self.bars else None

    @property
    def last_volume(self) -> Optional[float]:
        return self.bars[-1].v if self.bars else None

    @property
    def vwap(self) -> Optional[float]:
        if self._v <= 0:
            return None
        return self._pv / self._v

    @property
    def avg_volume_20(self) -> Optional[float]:
        """Mean volume of the last 20 completed bars (the latest bar included). None until 20 exist."""
        if len(self.bars) < VOL_WINDOW:
            return None
        return sum(b.v for b in self.bars[-VOL_WINDOW:]) / VOL_WINDOW

    @property
    def vol_ratio(self) -> Optional[float]:
        av = self.avg_volume_20
        if av is None or av <= 0 or not self.bars:
            return None
        return self.bars[-1].v / av

    @property
    def dev_pct(self) -> Optional[float]:
        vw = self.vwap
        if vw is None or vw <= 0 or not self.bars:
            return None
        return (self.bars[-1].c - vw) / vw * 100.0

    # ---- mutation ----------------------------------------------------------
    def update(self, new_bars: Iterable[Bar]) -> list[Bar]:
        """Append bars newer than last_ts, in order. Returns the bars actually added."""
        added = []
        for b in sorted(new_bars, key=lambda x: x.ts):
            if self.bars and b.ts <= self.bars[-1].ts:
                continue
            self.bars.append(b)
            self._pv += (b.h + b.l + b.c) / 3.0 * b.v
            self._v += b.v
            added.append(b)
        return added

    def reset(self) -> None:
        self.bars.clear()
        self._pv = self._v = 0.0


class DataState:
    """All symbols for the current session."""

    def __init__(self, symbols: Iterable[str]):
        self.symbols = list(symbols)
        self.states: dict[str, SymbolState] = {s: SymbolState(s) for s in self.symbols}

    def __getitem__(self, symbol: str) -> SymbolState:
        return self.states[symbol]

    def update(self, symbol: str, new_bars: Iterable[Bar]) -> list[Bar]:
        st = self.states.get(symbol)
        if st is None:
            st = self.states[symbol] = SymbolState(symbol)
            self.symbols.append(symbol)
        return st.update(new_bars)

    def update_many(self, bars_by_symbol: dict[str, list[Bar]]) -> dict[str, list[Bar]]:
        return {s: self.update(s, bs) for s, bs in bars_by_symbol.items()}

    def last_ts(self) -> Optional[datetime]:
        """Earliest last_ts across symbols that have bars (so no symbol is left behind on the next fetch)."""
        ts = [st.last_ts for st in self.states.values() if st.last_ts is not None]
        return min(ts) if ts else None

    def reset(self) -> None:
        for st in self.states.values():
            st.reset()
