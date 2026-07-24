import importlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from shared.global_context import InstrumentMetadata
from shared.polling import PollStatus

global_market = importlib.import_module("market-producer.global_market")
global_backfill = importlib.import_module("market-producer.global_backfill")


def _instrument(**overrides):
    values = {
        "instrument_key": "index:nikkei-225",
        "display_name": "Nikkei 225",
        "asset_class": "index",
        "currency": "JPY",
        "exchange": "JPX",
        "timezone": "Asia/Tokyo",
        "provider_alias": "^N225",
        "session_metadata": {
            "open": "09:00",
            "close": "15:30",
            "weekdays": [1, 2, 3, 4, 5],
        },
        "quote_convention": None,
    }
    values.update(overrides)
    return InstrumentMetadata(**values)


def _frame(index):
    return pd.DataFrame(
        [
            {
                "Open": 100.0,
                "High": 103.0,
                "Low": 99.0,
                "Close": 102.0,
                "Volume": 1234,
            }
        ],
        index=index,
    )


def test_daily_bars_use_session_close_and_convert_to_utc():
    adapter = global_market.YahooMarketDataAdapter()
    frame = _frame(
        pd.DatetimeIndex([pd.Timestamp("2026-07-23 00:00:00", tz="Asia/Tokyo")])
    )

    bars = adapter._normalize_frame(_instrument(), "1d", frame)

    assert len(bars) == 1
    assert bars[0].starts_at == datetime(
        2026,
        7,
        23,
        0,
        tzinfo=timezone.utc,
    )
    assert bars[0].ends_at == datetime(
        2026,
        7,
        23,
        6,
        30,
        tzinfo=timezone.utc,
    )
    assert bars[0].session_date.isoformat() == "2026-07-23"


def test_hourly_naive_provider_index_is_localized_to_instrument_timezone():
    adapter = global_market.YahooMarketDataAdapter()
    frame = _frame(pd.DatetimeIndex([pd.Timestamp("2026-07-23 09:00:00")]))

    bar = adapter._normalize_frame(_instrument(), "1h", frame)[0]

    assert bar.starts_at == datetime(
        2026,
        7,
        23,
        0,
        tzinfo=timezone.utc,
    )
    assert bar.ends_at == datetime(
        2026,
        7,
        23,
        1,
        tzinfo=timezone.utc,
    )


def test_fx_inverse_ohlc_preserves_high_low_and_direction():
    normalized = global_market.normalize_fx_ohlc(
        0.01,
        0.0125,
        0.008,
        0.009,
        provider_is_local_per_usd=False,
    )

    assert normalized == pytest.approx((100.0, 125.0, 80.0, 111.111111))
    assert normalized[1] >= max(normalized[0], normalized[3])
    assert normalized[2] <= min(normalized[0], normalized[3])


def test_adapter_skips_non_finite_rows():
    adapter = global_market.YahooMarketDataAdapter()
    frame = _frame(
        pd.DatetimeIndex([pd.Timestamp("2026-07-23 00:00:00", tz="Asia/Tokyo")])
    )
    frame.iloc[0, frame.columns.get_loc("Close")] = float("nan")

    assert adapter._normalize_frame(_instrument(), "1d", frame) == []


def test_empty_provider_response_is_normalized_to_empty(monkeypatch):
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    monkeypatch.setattr(global_market.yf, "Ticker", lambda _alias: ticker)

    bars = global_market.YahooMarketDataAdapter().fetch_bars(
        _instrument(),
        "1d",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    assert bars == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (global_market.YFRateLimitError(), PollStatus.RATE_LIMITED),
        (RuntimeError("provider down"), PollStatus.TRANSIENT_ERROR),
    ],
)
def test_runner_classifies_provider_failures(error, expected):
    adapter = MagicMock()
    adapter.fetch_bars.side_effect = error
    runner = global_market.GlobalContextMarketRunner(
        MagicMock(),
        adapter,
        list,
    )
    request = (
        _instrument(),
        "1d",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    bars, status = runner._fetch_one(request)

    assert bars == []
    assert status is expected


def test_daily_poll_is_session_and_dst_aware():
    instrument = _instrument(
        instrument_key="us-stock:NVDA",
        display_name="NVDA",
        asset_class="us_equity",
        currency="USD",
        exchange="Nasdaq",
        timezone="America/New_York",
        provider_alias="NVDA",
        session_metadata={
            "open": "09:30",
            "close": "16:00",
            "weekdays": [1, 2, 3, 4, 5],
        },
    )

    assert global_market.GlobalContextMarketRunner._daily_is_due(
        instrument,
        datetime(2026, 7, 23, 20, 31, tzinfo=timezone.utc),
        None,
    )
    assert not global_market.GlobalContextMarketRunner._daily_is_due(
        instrument,
        datetime(2026, 1, 23, 20, 31, tzinfo=timezone.utc),
        None,
    )
    assert global_market.GlobalContextMarketRunner._daily_is_due(
        instrument,
        datetime(2026, 1, 23, 21, 31, tzinfo=timezone.utc),
        None,
    )
    assert not global_market.GlobalContextMarketRunner._daily_is_due(
        instrument,
        datetime(2026, 7, 25, 21, 31, tzinfo=timezone.utc),
        None,
    )


@pytest.mark.asyncio
async def test_backfill_loads_us_symbols_directly_from_database(monkeypatch):
    class FakeDB:
        def __init__(self):
            self.ensured_symbols = None

        def list_active_symbols(self):
            return ["AAPL", "NVDA"]

        def ensure_us_equity_instruments(self, symbols):
            self.ensured_symbols = symbols

        def list_global_instruments(self):
            return []

    database = FakeDB()
    monkeypatch.setattr(global_backfill, "GLOBAL_CONTEXT_ENABLED", True)
    monkeypatch.setattr(global_backfill, "DB", lambda: database)

    await global_backfill.run_backfill()

    assert database.ensured_symbols == ["AAPL", "NVDA"]
