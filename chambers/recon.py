"""Daily broker reconciliation (PHASE1A §5.2) and nightly backups (§5.3).

Recon: after each equity session and after S2's daily rollover, compare

    actual   = Δ Alpaca equity since the previous recon
    expected = Σ realized gross P&L of every sleeve's trades closed since then
               + Δ unrealized P&L of the open positions (Alpaca's own unrealized_pl)
               − fees (the crypto commission on S2 fills in the window)

and store both in `recon`. |actual − expected| > max($5, 0.01% of equity) → status `mismatch` and an
alert with the numbers. The first recon only records the baseline. Recons chain: each one is measured
from the previous one of either kind, so the equity-session and crypto-rollover checks together cover
every hour without overlap.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("chambers.recon")

ABS_THRESHOLD = 5.0
PCT_THRESHOLD = 0.0001
BACKUPS_KEPT = 14


def run_recon(store, broker, now: datetime, kind: str, fee_rate: float = 0.0, notifier=None) -> dict:
    try:
        equity = float(broker.account()["equity"])
        positions = broker.positions()
    except Exception as e:
        store.write_recon(now, kind, "error", detail_json={"error": str(e)})
        log.warning("recon %s failed: %s", kind, e)
        return {"status": "error", "error": str(e)}
    unrealized = sum(float(p.get("unrealized_pl") or 0.0) for p in positions)
    prev = store.last_recon(with_equity=True)
    threshold = max(ABS_THRESHOLD, PCT_THRESHOLD * equity)
    if prev is None:
        store.write_recon(now, kind, "baseline", equity=equity, unrealized=unrealized, threshold=threshold,
                          detail_json={"positions": len(positions)})
        return {"status": "baseline", "equity": equity, "unrealized": unrealized}
    prev_ts = datetime.fromisoformat(prev["ts"])
    closed = store.closed_trades_between(None, prev_ts, now)
    realized = sum(t.gross_pnl or 0.0 for t in closed)
    fees = 0.0
    if fee_rate:
        for t in closed:
            if "/" in t.symbol:
                fees += fee_rate * t.qty * (t.exit_price or 0.0)
                if prev_ts <= datetime.fromisoformat(t.entry_ts) < now:
                    fees += fee_rate * t.qty * t.entry_price
        for t in store.open_trades(None):
            if "/" in t.symbol and prev_ts <= datetime.fromisoformat(t.entry_ts) < now:
                fees += fee_rate * t.qty * t.entry_price
    prev_u = prev.get("unrealized") or 0.0
    actual = equity - prev["equity"]
    expected = realized + (unrealized - prev_u) - fees
    diff = actual - expected
    status = "pass" if abs(diff) <= threshold else "mismatch"
    by_sleeve: dict[str, float] = {}
    for t in closed:
        by_sleeve[t.sleeve_id] = round(by_sleeve.get(t.sleeve_id, 0.0) + (t.gross_pnl or 0.0), 2)
    detail = {"closed_trades": len(closed), "realized_gross_by_sleeve": by_sleeve, "positions": len(positions)}
    store.write_recon(now, kind, status, equity=equity, prev_ts=prev["ts"], prev_equity=prev["equity"],
                      actual_delta=round(actual, 2), realized_gross=round(realized, 2), unrealized=unrealized,
                      prev_unrealized=prev_u, fees=round(fees, 2), expected_delta=round(expected, 2),
                      diff=round(diff, 2), threshold=round(threshold, 2), detail_json=detail)
    out = {"status": status, "actual": actual, "expected": expected, "diff": diff, "threshold": threshold,
           "realized": realized, "d_unrealized": unrealized - prev_u, "fees": fees}
    if status == "mismatch" and notifier is not None:
        notifier.alert(f"recon:{kind}", (
            f"RECON MISMATCH ({kind.replace('_', ' ')}): equity change {actual:+,.2f} vs expected {expected:+,.2f} "
            f"(realized {realized:+,.2f}, unrealized change {unrealized - prev_u:+,.2f}, fees {fees:,.2f}). "
            f"Difference {diff:+,.2f} > {threshold:,.2f}."), now)
    log.info("recon %s: %s actual=%.2f expected=%.2f diff=%.2f", kind, status, actual, expected, diff)
    return out


def nightly_backup(store, backup_dir: Path, d: date, keep: int = BACKUPS_KEPT) -> str:
    """`data/backups/chambers-YYYY-MM-DD.db` via the sqlite backup API; keep the newest `keep`."""
    backup_dir = Path(backup_dir)
    dest = store.backup_to(backup_dir / f"chambers-{d.isoformat()}.db")
    olds = sorted(p for p in backup_dir.glob("chambers-????-??-??.db"))
    for p in olds[:-keep] if keep > 0 else []:
        try:
            os.remove(p)
        except OSError as e:
            log.warning("could not remove old backup %s: %s", p, e)
    return dest


def backup_exists(backup_dir: Path, d: date) -> bool:
    return (Path(backup_dir) / f"chambers-{d.isoformat()}.db").exists()
