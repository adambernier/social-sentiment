from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from synthetic_nav import (
    HOLDINGS,
    NAV_SYMBOL,
    SyntheticNavRunner,
    estimate_pct_change,
    is_final_close,
    official_close_timestamp,
    previous_trading_date,
    proxy_change,
)

ET = ZoneInfo("America/New_York")

SESSION = date(2026, 7, 8)  # a Wednesday


def _series(prices: dict[str, float]) -> pd.Series:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in prices])
    return pd.Series(list(prices.values()), index=idx)


def _proxy_frame(last_by_ticker: dict[str, float], prev: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-07"), pd.Timestamp(SESSION)])
    return pd.DataFrame(
        {ticker: [prev, last] for ticker, last in last_by_ticker.items()}, index=idx
    )


def _calendar(days: list[date]) -> MagicMock:
    cal = MagicMock()

    def schedule(start_date, end_date):
        sel = [d for d in days if start_date <= d <= end_date]
        return pd.DataFrame(index=pd.DatetimeIndex([pd.Timestamp(d) for d in sel]))

    cal.schedule.side_effect = schedule
    return cal


def _runner(days: list[date], db=None) -> SyntheticNavRunner:
    return SyntheticNavRunner(
        db=db or MagicMock(),
        limiter=MagicMock(),
        backoff=MagicMock(),
        calendar=_calendar(days),
    )


# --- proxy_change ---

def test_proxy_change_normal():
    s = _series({"2026-07-06": 100.0, "2026-07-07": 101.0, "2026-07-08": 102.0})
    assert proxy_change(s, SESSION) == pytest.approx((102.0 - 101.0) / 101.0 * 100)

def test_proxy_change_rejects_stale_last_bar():
    s = _series({"2026-07-06": 100.0, "2026-07-07": 101.0})
    with pytest.raises(RuntimeError, match="no bar"):
        proxy_change(s, SESSION)

def test_proxy_change_rejects_implausible_move():
    s = _series({"2026-07-07": 100.0, "2026-07-08": 109.0})
    with pytest.raises(RuntimeError, match="implausible"):
        proxy_change(s, SESSION)

def test_proxy_change_rejects_insufficient_history():
    s = _series({"2026-07-08": 102.0})
    with pytest.raises(RuntimeError, match="insufficient"):
        proxy_change(s, SESSION)

def test_proxy_change_rejects_empty_series():
    with pytest.raises(RuntimeError, match="no price data"):
        proxy_change(pd.Series(dtype=float), SESSION)


# --- estimate_pct_change ---

def test_estimate_all_proxies_priced():
    last = {t: 100.0 for t in HOLDINGS}
    last["SCHB"] = 101.0  # +1% on a 0.55 weight, everything else flat
    est = estimate_pct_change(_proxy_frame(last), SESSION)
    assert est == pytest.approx(0.55)

def test_estimate_renormalizes_over_priced_weights():
    last = {t: 100.0 for t in HOLDINGS if t != "SCHF"}
    last["SCHB"] = 101.0
    est = estimate_pct_change(_proxy_frame(last), SESSION)
    # SCHF (0.27) missing entirely -> renormalize over the remaining 0.73
    assert est == pytest.approx(0.55 / 0.73)

def test_estimate_none_when_nothing_priced():
    assert estimate_pct_change(pd.DataFrame(), SESSION) is None


# --- is_final_close / official_close_timestamp / previous_trading_date ---

def test_is_final_close_prior_date_always_final():
    now_et = datetime(2026, 7, 8, 10, 0, tzinfo=ET)
    assert is_final_close(date(2026, 7, 7), now_et)

def test_is_final_close_same_day_only_after_publication():
    d = date(2026, 7, 8)
    assert not is_final_close(d, datetime(2026, 7, 8, 17, 0, tzinfo=ET))
    assert not is_final_close(d, datetime(2026, 7, 8, 18, 14, tzinfo=ET))
    assert is_final_close(d, datetime(2026, 7, 8, 18, 15, tzinfo=ET))

def test_is_final_close_future_date_never_final():
    now_et = datetime(2026, 7, 8, 23, 0, tzinfo=ET)
    assert not is_final_close(date(2026, 7, 9), now_et)

def test_official_close_timestamp_dst():
    assert official_close_timestamp(date(2026, 7, 8)) == datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    assert official_close_timestamp(date(2026, 1, 8)) == datetime(2026, 1, 8, 21, 0, tzinfo=timezone.utc)

def test_previous_trading_date_skips_gaps():
    days = [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6), date(2026, 7, 7), SESSION]
    cal = _calendar(days)
    assert previous_trading_date(cal, SESSION) == date(2026, 7, 7)
    # Monday's previous trading day is the prior Thursday (7/3 holiday, weekend)
    assert previous_trading_date(cal, date(2026, 7, 6)) == date(2026, 7, 2)

def test_previous_trading_date_none_without_history():
    assert previous_trading_date(_calendar([]), SESSION) is None


# --- SyntheticNavRunner._upsert_official_rows ---

def _inserted_quotes(db) -> list:
    return [c.args[0] for c in db.insert_quote.call_args_list]

def test_upsert_backdates_official_rows():
    db = MagicMock()
    runner = _runner([date(2026, 7, 6), date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-06": 20.00, "2026-07-07": 20.10})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 10, 0, tzinfo=ET))

    quotes = _inserted_quotes(db)
    assert [(q.symbol, q.timestamp, q.price, q.volume, q.market_session) for q in quotes] == [
        (NAV_SYMBOL, official_close_timestamp(date(2026, 7, 6)), 20.00, 0, "regular"),
        (NAV_SYMBOL, official_close_timestamp(date(2026, 7, 7)), 20.10, 0, "regular"),
    ]
    assert runner._official_done_for is None  # today's NAV not published yet

def test_upsert_skips_non_trading_dates():
    db = MagicMock()
    runner = _runner([date(2026, 7, 6), SESSION], db=db)  # 7/7 not a trading day
    navs = _series({"2026-07-06": 20.00, "2026-07-07": 20.10})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 10, 0, tzinfo=ET))
    assert [q.timestamp for q in _inserted_quotes(db)] == [official_close_timestamp(date(2026, 7, 6))]

def test_upsert_defers_today_until_publication():
    db = MagicMock()
    runner = _runner([date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-07": 20.10, "2026-07-08": 20.25})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 17, 0, tzinfo=ET))
    assert [q.price for q in _inserted_quotes(db)] == [20.10]
    assert runner._official_done_for is None

def test_upsert_stores_today_after_publication():
    db = MagicMock()
    runner = _runner([date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-07": 20.10, "2026-07-08": 20.25})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 18, 30, tzinfo=ET))
    assert [q.price for q in _inserted_quotes(db)] == [20.10, 20.25]
    assert runner._official_done_for == SESSION

def test_upsert_defers_exact_repeat_of_prior_close():
    db = MagicMock()
    runner = _runner([date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-07": 20.10, "2026-07-08": 20.10})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 18, 30, tzinfo=ET))
    assert [q.price for q in _inserted_quotes(db)] == [20.10]
    assert runner._official_done_for is None  # retried next cycle / next morning

def test_upsert_skips_implausible_day_move():
    db = MagicMock()
    runner = _runner([date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-07": 20.00, "2026-07-08": 25.00})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 18, 30, tzinfo=ET))
    assert [q.price for q in _inserted_quotes(db)] == [20.00]

def test_upsert_idempotent_when_row_exists():
    db = MagicMock()
    db.insert_quote.return_value = False  # ON CONFLICT no-op
    runner = _runner([date(2026, 7, 7), SESSION], db=db)
    navs = _series({"2026-07-07": 20.10, "2026-07-08": 20.25})
    runner._upsert_official_rows(navs, datetime(2026, 7, 8, 18, 30, tzinfo=ET))
    assert runner._official_done_for == SESSION  # already stored still counts as done


# --- SyntheticNavRunner._insert_estimate ---

def test_insert_estimate_prices_off_baseline():
    db = MagicMock()
    runner = _runner([SESSION], db=db)
    now_utc = datetime(2026, 7, 8, 15, 0, 42, 123456, tzinfo=timezone.utc)  # 11:00 ET
    runner._insert_estimate(date(2026, 7, 7), 20.0, 0.5, now_utc, SESSION)

    (quote,) = _inserted_quotes(db)
    assert quote.price == pytest.approx(20.1)
    assert quote.timestamp == now_utc.replace(second=0, microsecond=0)
    assert quote.volume == 0
    assert quote.market_session == "regular"

def test_insert_estimate_never_collides_with_official_close():
    db = MagicMock()
    runner = _runner([SESSION], db=db)
    runner._insert_estimate(date(2026, 7, 7), 20.0, 0.5, official_close_timestamp(SESSION), SESSION)
    db.insert_quote.assert_not_called()
