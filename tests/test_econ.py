"""Econ calendar and news-day tagging (PHASE1A §5.4)."""
from datetime import date, datetime, timedelta

from chambers.clock import ET
from chambers.econ import CALENDAR_PATH, load_calendar, load_into_store, news_split
from chambers.engine import Engine
from chambers.store import Store
from chambers.strategy import Params
from chambers.twin import LiveTwin, TwinCtx

from .mocks import FakeTime, MockBroker, make_clock

T = datetime(2026, 10, 14, 10, 0, tzinfo=ET)       # CPI day in the seeded calendar


def test_seeded_calendar_is_valid_and_covers_six_months():
    ev = load_calendar(CALENDAR_PATH)
    dates = sorted(e["date"] for e in ev)
    assert len(ev) >= 20 and dates[0] <= "2026-10-02" and dates[-1] >= "2027-03-17"
    kinds = {e["event"].split(" (")[0] for e in ev}
    assert {"FOMC decision", "CPI", "Employment Situation", "GDP advance"} <= kinds
    assert any(k.startswith("PCE") for k in kinds)
    assert all(e["source"] for e in ev)
    text = CALENDAR_PATH.read_text()
    assert "Sources:" in text and "bls.gov" in text and "federalreserve.gov" in text and "bea.gov" in text


def test_invalid_calendar_keeps_previous_and_logs(tmp_path):
    st = Store(tmp_path / "t.db")
    assert load_into_store(st) >= 20
    bad = tmp_path / "bad.yaml"
    bad.write_text("events:\n  - date: not-a-date\n    event: X\n")
    assert load_into_store(st, bad) == 0
    assert st.econ_events_on("2026-10-14")[0]["event"] == "CPI (Sep)"
    assert st.errors_count(where="econ.calendar") == 1
    assert load_into_store(st, tmp_path / "missing.yaml") == 0


def test_trades_and_twin_trades_tagged_and_split(tmp_path):
    st = Store(tmp_path / "t.db")
    load_into_store(st)
    # a Phase 0 row with no tag gets backfilled
    old = st.open_trade("AAPL", "long", 1, T - timedelta(days=1), 100, "o", None, None, {}, {})
    st.close_trade(old, T - timedelta(days=1) + timedelta(minutes=5), 101, "x", None, None, "vwap_touch", 1, 0, 1, 1, 0, 1)
    st._exec("UPDATE trades SET news_day=NULL, event=NULL")
    assert st.backfill_news_tags() == 1 and st.get_trade(old).news_day == 0
    # live twin trades take the tag of their entry day
    broker = MockBroker()
    eng = Engine(st, broker, make_clock(FakeTime(T), broker), ["AAPL"], Params().to_dict())
    tw = LiveTwin(eng, allow_short=False, fallback_rate=lambda: 1.0)
    tw.book.p = 1.0
    ctx = TwinCtx(T, 100.0, True, exit=lambda pos: None, size=lambda side, px: 3)
    tw.on_cycle_bars(eng, T, {"AAPL": ctx}, True)
    tw.book.flatten(T + timedelta(minutes=10), {"AAPL": 99.0})
    tt = st.all_closed_twin_trades()[0]
    assert tt["news_day"] == 1 and tt["event"] == "CPI (Sep)"
    tid = st.open_trade("AAPL", "long", 1, T, 100, "o", None, None, {}, {}, news_day=True, event="CPI (Sep)")
    st.close_trade(tid, T + timedelta(minutes=5), 102, "x", None, None, "vwap_touch", 1, 0, 1, 2, 0, 2)
    sp = news_split(st, "S0", "2026-10-13", "2026-10-14")
    assert sp["sleeve"] == {"news": {"trades": 1, "net": 2.0}, "non_news": {"trades": 1, "net": 1.0}}
    assert sp["twin"]["news"]["trades"] == 1
