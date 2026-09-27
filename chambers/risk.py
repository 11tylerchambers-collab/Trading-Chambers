"""Position sizing and portfolio circuit breakers (PHASE1A §3).

Sizing (S1, S2, S3): qty = sleeve_capital × risk_pct / stop_distance, where stop_distance is the
per-unit distance from entry to the ATR stop; notional capped at max_notional_pct × sleeve capital.
Equities: floor, minimum 1 share, skipped (`notional_cap`) if one share already exceeds the cap.
Crypto: fractional (8 dp, rounded down), skipped below the minimum order size. S0 keeps Phase 0's
fixed notional.

Circuit breakers, shared by every sleeve through one `Portfolio`:
  - daily loss: account equity now vs. at the session open; below −daily_loss_pct → no new entries in
    the equity sleeves (S0, S1, S3) for the rest of the session. Exits continue. One alert. The halt is
    stored in `risk_days`, so a restart keeps it.
  - same side: at most max_same_side open positions in one direction across all sleeves.
  - no leverage: total gross exposure (open trades at entry notional) + the new entry ≤ account equity.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("chambers.risk")

CRYPTO_MIN_QTY = 0.0001        # Alpaca's minimum BTC/USD order size


def risk_qty(capital: float, risk_pct: float, stop_distance: Optional[float], price: float,
             max_notional_pct: float, fractional: bool = False,
             min_qty: float = CRYPTO_MIN_QTY) -> tuple[float, Optional[str]]:
    """(qty, skip_reason). skip_reason is None when qty > 0."""
    if not stop_distance or stop_distance <= 0 or price <= 0 or capital <= 0:
        return 0, "no_stop"
    raw = capital * risk_pct / stop_distance
    cap = capital * max_notional_pct
    if fractional:
        qty = min(raw, cap / price)
        qty = math.floor(qty * 1e8) / 1e8
        if qty < min_qty:
            return 0, "notional_cap"
        return qty, None
    cap_shares = math.floor(cap / price)
    if cap_shares < 1:
        return 0, "notional_cap"
    qty = max(1, math.floor(raw))
    return min(qty, cap_shares), None


class Portfolio:
    def __init__(self, store, broker, clock, daily_loss_pct: float = 0.02, max_same_side: int = 15,
                 notifier=None, cache_s: float = 30.0, monotonic: Callable[[], float] = time.monotonic):
        self.store = store
        self.broker = broker
        self.clock = clock
        self.daily_loss_pct = daily_loss_pct
        self.max_same_side = max_same_side
        self.notifier = notifier
        self.cache_s = cache_s
        self._mono = monotonic
        self._eq: Optional[float] = None
        self._eq_at: float = -1e18

    # ---- account equity (cached; one API call per cache_s at most) -------------
    def equity(self, fresh: bool = False) -> Optional[float]:
        if not fresh and self._eq is not None and self._mono() - self._eq_at < self.cache_s:
            return self._eq
        try:
            self._eq = float(self.broker.account()["equity"])
            self._eq_at = self._mono()
        except Exception as e:  # BrokerError is already in store.errors
            log.warning("account() failed: %s", e)
        return self._eq

    # ---- daily loss limit ----------------------------------------------------
    def open_equity(self, now: datetime) -> Optional[float]:
        """Equity at the session open: the first value seen at or after the open, stored in risk_days."""
        s = self.clock.today_session()
        if s is None or now < s.open:
            return None
        rd = self.store.risk_day(s.date)
        if rd and rd.get("open_equity") is not None:
            return rd["open_equity"]
        eq = self.equity()
        if eq is None:
            return None
        self.store.set_risk_open(s.date, eq, now)
        return eq

    def daily_pnl(self, now: datetime) -> Optional[tuple[float, float]]:
        """(P&L since the session open, open equity), or None outside a session / without equity."""
        oe = self.open_equity(now)
        eq = self.equity()
        if oe is None or eq is None:
            return None
        return eq - oe, oe

    def halted(self, now: datetime) -> bool:
        s = self.clock.today_session()
        if s is None:
            return False
        rd = self.store.risk_day(s.date)
        return bool(rd and rd["halted"])

    def check_daily_loss(self, now: datetime) -> bool:
        """True if equity-sleeve entries are halted for this session (trips the halt when the limit is hit)."""
        s = self.clock.today_session()
        if s is None or now < s.open or now >= s.close:
            return False
        if self.halted(now):
            return True
        got = self.daily_pnl(now)
        if got is None:
            return False
        pnl, oe = got
        limit = -self.daily_loss_pct * oe
        if pnl < limit:
            detail = {"pnl": round(pnl, 2), "open_equity": round(oe, 2), "limit": round(limit, 2),
                      "daily_loss_pct": self.daily_loss_pct}
            self.store.set_risk_halt(s.date, now, detail)
            self.store.log_error("risk.daily_loss", f"daily loss limit tripped: P&L since open {pnl:.2f} < "
                                 f"{limit:.2f} ({self.daily_loss_pct:.1%} of {oe:.2f}); equity-sleeve entries halted "
                                 f"for the rest of the session", None, now)
            if self.notifier is not None:
                self.notifier.alert("daily_loss", f"DAILY LOSS LIMIT tripped at {now.strftime('%H:%M')} ET: "
                                                  f"P&L since open {pnl:,.2f} (limit {limit:,.2f}). New entries in "
                                                  f"S0/S1/S3 halted for the session; exits continue.", now)
            return True
        return False

    # ---- entry check ---------------------------------------------------------
    def exposure(self) -> tuple[float, dict[str, int]]:
        """(gross exposure at entry notional, open positions per side) across all sleeves."""
        gross = 0.0
        sides = {"long": 0, "short": 0}
        for t in self.store.open_trades(None):
            gross += abs(t.qty * t.entry_price)
            sides[t.side] = sides.get(t.side, 0) + 1
        return gross, sides

    def allow_entry(self, sleeve, symbol: str, side: str, notional: float, now: datetime) -> Optional[str]:
        if getattr(sleeve, "equity_sleeve", True) and self.check_daily_loss(now):
            return "daily_loss_halt"
        gross, sides = self.exposure()
        if sides.get(side, 0) >= self.max_same_side:
            return "same_side_cap"
        eq = self.equity()
        if eq is not None and gross + abs(notional) > eq:
            return "exposure_cap"
        return None
