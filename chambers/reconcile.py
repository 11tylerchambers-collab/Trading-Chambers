"""Reconcile broker positions against the open trades of one or more sleeves (spec §8; PHASE1A §10).

The broker holds one net quantity per symbol; the sleeves' books hold one open trade per symbol per
sleeve. So reconcile works per symbol across all sleeves, then hands each sleeve its own outcome:

  - one trade for the symbol (the Phase 0 case): same sign as the broker → adopt (quantity is not
    compared, as in Phase 0); otherwise the trade is closed as `reconcile_missing` and whatever the
    broker holds is flattened as an orphan;
  - several trades (several sleeves): the broker net equals their sum → adopt all; the broker holds
    nothing → all `reconcile_missing`; exactly one trade explains the broker quantity → adopt it, the
    others are missing; otherwise all are missing and the broker quantity is flattened as an orphan;
  - a broker position no trade explains → orphan: flatten immediately and log (`where_ = reconcile`).

Crypto quantities are compared within 1% because Alpaca takes the crypto fee out of the coins received.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from .broker import BrokerError, is_crypto

log = logging.getLogger("chambers.reconcile")


def _same(a: float, b: float, symbol: str) -> bool:
    if is_crypto(symbol):
        return abs(a - b) <= 0.01 * max(abs(a), abs(b)) + 1e-9
    return abs(a - b) < 1e-6


def decide(trades: list, broker_qty: float, symbol: str) -> tuple[list, list, float]:
    """(adopt, missing, orphan_qty) for one symbol."""
    if len(trades) == 1:
        t = trades[0]
        if broker_qty and (broker_qty > 0) == (t.side == "long"):
            return [t], [], 0.0
        return [], [t], broker_qty
    expected = sum(t.signed_qty for t in trades)
    if _same(broker_qty, expected, symbol):
        return list(trades), [], 0.0
    if not broker_qty:
        return [], list(trades), 0.0
    one = [t for t in trades if _same(t.signed_qty, broker_qty, symbol)]
    if one:
        return [one[0]], [t for t in trades if t is not one[0]], 0.0
    return [], list(trades), broker_qty


def reconcile_sleeves(sleeves: list, store, broker, now: datetime,
                      log_error: Callable[[str, BaseException], None]) -> dict:
    result: dict = {"adopted": [], "orphans": [], "missing": [],
                    "by_sleeve": {s.sleeve_id: {"adopted": [], "missing": []} for s in sleeves}}
    try:
        broker_pos = {p["symbol"]: p for p in broker.positions() if abs(p["qty"]) > 0}
    except BrokerError as e:
        log_error("reconcile", e)
        return result
    by_id = {s.sleeve_id: s for s in sleeves}
    by_symbol: dict[str, list] = {}
    for t in store.open_trades(None):
        if t.sleeve_id in by_id:
            by_symbol.setdefault(t.symbol, []).append(t)
    for s in sleeves:
        s.positions = {}
    orphans: dict[str, float] = {}
    for sym, trades in by_symbol.items():
        bp = broker_pos.pop(sym, None)
        adopt, missing, orphan_qty = decide(trades, bp["qty"] if bp else 0.0, sym)
        for t in adopt:
            sl = by_id[t.sleeve_id]
            sl.positions[sym] = sl.adopt(t)
            result["adopted"].append(sym)
            result["by_sleeve"][t.sleeve_id]["adopted"].append(sym)
            log.info("reconcile: %s adopted %s %s x%s", t.sleeve_id, t.side, sym, t.qty)
        for t in missing:
            by_id[t.sleeve_id].close_missing(t, now)
            result["missing"].append(sym)
            result["by_sleeve"][t.sleeve_id]["missing"].append(sym)
        if orphan_qty:
            orphans[sym] = orphan_qty
    for sym, bp in broker_pos.items():
        orphans[sym] = bp["qty"]
    executor = sleeves[0]
    for sym, q in orphans.items():
        # broker position with no open trade behind it → orphan: flatten immediately
        qty = abs(q) if is_crypto(sym) else int(abs(q))
        side = "sell" if q > 0 else "buy"
        try:
            oid = broker.submit_market(sym, qty, side)
            fill = executor.wait_fill(oid)
            store.log_error("reconcile", f"orphan position {sym} qty {q} flattened "
                            f"(order {oid}, status {fill.get('status')})", None, now)
        except BrokerError as e:
            store.log_error("reconcile", f"orphan position {sym} qty {q} could not be "
                            f"flattened: {e}", None, now)
        result["orphans"].append(sym)
    return result
