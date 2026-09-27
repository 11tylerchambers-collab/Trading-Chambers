"""News-day calendar (PHASE1A §5.4).

`config/econ_calendar.yaml` lists dated high-impact US events (FOMC decisions, CPI, jobs report,
PCE, GDP advance); its header names the sources. The user edits it; the engine loads it into
`econ_events` at startup and again every night, so an edit takes effect by the next session without
a restart. A date with any event is a news day. Every trade, twin trade and P100 trade is tagged at
entry with `news_day` and `event`; `news_split` gives the reports' news / non-news breakdown.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("chambers.econ")

CALENDAR_PATH = Path(__file__).resolve().parent.parent / "config" / "econ_calendar.yaml"


def load_calendar(path: Path = CALENDAR_PATH) -> list[dict]:
    """Parse and validate the YAML. Raises ValueError with the offending entry."""
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out = []
    for i, e in enumerate(doc.get("events") or []):
        if not isinstance(e, dict) or "date" not in e or not e.get("event"):
            raise ValueError(f"econ calendar entry {i} needs 'date' and 'event': {e!r}")
        d = e["date"]
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if not isinstance(d, date):
            raise ValueError(f"econ calendar entry {i}: bad date {e['date']!r}")
        out.append({"date": d.isoformat(), "event": str(e["event"]), "time": str(e["time"]) if e.get("time") else None,
                    "source": e.get("source"), "confirmed": e.get("confirmed")})
    return out


def load_into_store(store, path: Path = CALENDAR_PATH, runners: Optional[list] = None) -> int:
    """Replace `econ_events` from the YAML, tag any untagged rows, and drop the sleeves' per-day cache.
    Returns the number of events (0 and a warning if the file is missing or invalid)."""
    try:
        events = load_calendar(path)
    except FileNotFoundError:
        log.warning("econ calendar %s not found: no news days", path)
        return 0
    except (ValueError, yaml.YAMLError) as e:
        log.error("econ calendar %s is invalid, keeping the previous one: %s", path, e)
        store.log_error("econ.calendar", f"invalid econ calendar: {e}", None)
        return 0
    n = store.replace_econ_events(events)
    store.backfill_news_tags()
    for r in runners or []:
        if hasattr(r, "_news_cache"):
            r._news_cache.clear()
    return n


def news_split(store, sleeve_id: Optional[str], start: str, end: str) -> dict:
    """Closed LIVE trades and twin trades between two ET dates, split by news day."""
    def agg(rows):
        out = {"news": {"trades": 0, "net": 0.0}, "non_news": {"trades": 0, "net": 0.0}}
        for news_day, net in rows:
            k = "news" if news_day else "non_news"
            out[k]["trades"] += 1
            out[k]["net"] = round(out[k]["net"] + (net or 0.0), 2)
        return out
    return {"sleeve": agg(store.news_net_rows("trades", sleeve_id, start, end)),
            "twin": agg(store.news_net_rows("twin_trades", sleeve_id, start, end))}
