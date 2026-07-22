import importlib
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pandas as pd
import pytest

from shared.polling import PollOutcome, PollStatus


ROOT = Path(__file__).resolve().parent.parent

# The source directory is named ``storage-service`` but the market container
# copies it as ``storage_service``. Mirror that package layout for unit tests.
if "storage_service" not in sys.modules:
    storage_service = ModuleType("storage_service")
    storage_service.__path__ = [str(ROOT / "storage-service")]
    sys.modules["storage_service"] = storage_service

# Keep local unit tests usable when only the common test dependencies are
# installed. CI and the service image exercise the real calendar dependency.
try:
    import pandas_market_calendars  # noqa: F401
except ModuleNotFoundError:
    calendar_module = ModuleType("pandas_market_calendars")
    calendar_module.get_calendar = lambda _name: MagicMock()
    sys.modules["pandas_market_calendars"] = calendar_module


market = importlib.import_module("market-producer.main")
reddit = importlib.import_module("reddit-producer.main")


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, json_error=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def test_poll_outcome_rejects_retry_after_without_rate_limit():
    with pytest.raises(ValueError, match="requires a rate-limited outcome"):
        PollOutcome(PollStatus.SUCCESS, retry_after_seconds=60)


def test_market_empty_data_is_not_a_rate_limit(monkeypatch):
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    monkeypatch.setattr(market, "get_market_session", lambda _now: "regular")
    monkeypatch.setattr(market.yf, "Ticker", lambda _symbol: ticker)

    quote, status = market.fetch_single_quote(
        "PLTR",
        datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert quote is None
    assert status is PollStatus.NO_DATA


def test_market_yfinance_rate_limit_is_classified_explicitly(monkeypatch):
    class FakeRateLimit(Exception):
        pass

    ticker = MagicMock()
    ticker.history.side_effect = FakeRateLimit("slow down")
    monkeypatch.setattr(market, "YFRateLimitError", FakeRateLimit)
    monkeypatch.setattr(market, "get_market_session", lambda _now: "regular")
    monkeypatch.setattr(market.yf, "Ticker", lambda _symbol: ticker)

    quote, status = market.fetch_single_quote(
        "PLTR",
        datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert quote is None
    assert status is PollStatus.RATE_LIMITED


def test_market_other_provider_failure_is_transient(monkeypatch):
    ticker = MagicMock()
    ticker.history.side_effect = RuntimeError("provider unavailable")
    monkeypatch.setattr(market, "get_market_session", lambda _now: "regular")
    monkeypatch.setattr(market.yf, "Ticker", lambda _symbol: ticker)

    quote, status = market.fetch_single_quote(
        "PLTR",
        datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert quote is None
    assert status is PollStatus.TRANSIENT_ERROR


@pytest.mark.asyncio
async def test_market_backoff_and_metric_only_count_rate_limits(monkeypatch):
    symbols = ["RATE", "ERROR", "EMPTY"]
    monkeypatch.setattr(market, "tickers", lambda: symbols)
    monkeypatch.setattr(market, "all_polled_futures", lambda: [])
    gather = AsyncMock(
        return_value=[
            (None, PollStatus.RATE_LIMITED),
            (None, PollStatus.TRANSIENT_ERROR),
            (None, PollStatus.NO_DATA),
        ]
    )
    monkeypatch.setattr(market, "paced_gather", gather)
    rate_limit_metric = MagicMock()
    monkeypatch.setattr(market, "RATE_LIMITS_HIT_TOTAL", rate_limit_metric)
    backoff = MagicMock()
    backoff.due.return_value = symbols

    await market.fetch_and_store(MagicMock(), MagicMock(), backoff)

    assert backoff.record.call_args_list == [
        call("RATE", True),
        call("ERROR", False),
        call("EMPTY", False),
    ]
    rate_limit_metric.labels.assert_called_once_with(platform="market")
    rate_limit_metric.labels.return_value.inc.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "headers", "expected_status", "expected_retry_after"),
    [
        (403, {}, PollStatus.BLOCKED, None),
        (429, {"Retry-After": "120"}, PollStatus.RATE_LIMITED, 120),
        (503, {}, PollStatus.TRANSIENT_ERROR, None),
        (401, {}, PollStatus.ERROR, None),
    ],
)
async def test_reddit_http_statuses_have_distinct_outcomes(
    status_code,
    headers,
    expected_status,
    expected_retry_after,
):
    client = MagicMock()
    client.get = AsyncMock(
        return_value=FakeResponse(status_code, headers=headers)
    )

    outcome = await reddit.fetch_and_process(client, MagicMock(), deque())

    assert outcome.status is expected_status
    assert outcome.retry_after_seconds == expected_retry_after


@pytest.mark.asyncio
async def test_reddit_request_failure_is_transient():
    request = httpx.Request("GET", reddit.FEED_URL)
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=httpx.ConnectError("connection failed", request=request)
    )

    outcome = await reddit.fetch_and_process(client, MagicMock(), deque())

    assert outcome.status is PollStatus.TRANSIENT_ERROR


@pytest.mark.asyncio
async def test_reddit_invalid_json_is_transient():
    client = MagicMock()
    client.get = AsyncMock(
        return_value=FakeResponse(200, json_error=ValueError("invalid JSON"))
    )

    outcome = await reddit.fetch_and_process(client, MagicMock(), deque())

    assert outcome.status is PollStatus.TRANSIENT_ERROR


@pytest.mark.parametrize(
    ("outcome", "current_backoff", "jitter", "expected_delay"),
    [
        (PollOutcome(PollStatus.RATE_LIMITED, 120), 900, 0, 120),
        (PollOutcome(PollStatus.RATE_LIMITED), 900, 0, 1800),
        (PollOutcome(PollStatus.BLOCKED), 900, 0, 1800),
        (PollOutcome(PollStatus.TRANSIENT_ERROR), 900, 5, 65),
        (PollOutcome(PollStatus.SUCCESS), 1800, 0, 900),
        (PollOutcome(PollStatus.ERROR), 1800, 0, 900),
    ],
)
def test_reddit_retry_policy(
    monkeypatch,
    outcome,
    current_backoff,
    jitter,
    expected_delay,
):
    monkeypatch.setattr(reddit, "POLL_INTERVAL", 900)
    monkeypatch.setattr(reddit, "MAX_BACKOFF", 3600)
    monkeypatch.setattr(reddit, "TRANSIENT_RETRY_INTERVAL", 60)
    monkeypatch.setattr(reddit, "TRANSIENT_RETRY_JITTER", 15)

    delay = reddit.next_poll_delay(
        outcome,
        current_backoff,
        jitter_seconds=jitter,
    )

    assert delay == expected_delay


def reddit_comment_response():
    return FakeResponse(
        200,
        payload={
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "comment-1",
                            "body": "PLTR earnings look strong",
                            "created_utc": 1_774_300_000,
                            "score": 4,
                        }
                    }
                ]
            }
        },
    )


@pytest.mark.asyncio
async def test_reddit_publish_failure_leaves_comment_retryable(monkeypatch):
    client = MagicMock()
    client.get = AsyncMock(return_value=reddit_comment_response())
    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock(
        side_effect=RuntimeError("broker unavailable")
    )
    processed_ids = deque(maxlen=10)
    monkeypatch.setattr(reddit, "keywords_map", lambda: {"PLTR": []})
    monkeypatch.setattr(reddit, "match_symbol", lambda _body, _symbol: True)

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await reddit.fetch_and_process(client, channel, processed_ids)

    assert list(processed_ids) == []


@pytest.mark.asyncio
async def test_reddit_successful_publish_advances_processed_id(monkeypatch):
    client = MagicMock()
    client.get = AsyncMock(return_value=reddit_comment_response())
    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock()
    processed_ids = deque(maxlen=10)
    post_metric = MagicMock()
    monkeypatch.setattr(reddit, "keywords_map", lambda: {"PLTR": []})
    monkeypatch.setattr(reddit, "match_symbol", lambda _body, _symbol: True)
    monkeypatch.setattr(reddit, "POSTS_INGESTED_TOTAL", post_metric)

    outcome = await reddit.fetch_and_process(client, channel, processed_ids)

    assert outcome.status is PollStatus.SUCCESS
    assert list(processed_ids) == ["comment-1"]
    channel.default_exchange.publish.assert_awaited_once()
    post_metric.labels.assert_called_once_with(platform="reddit", symbol="PLTR")
