import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError


try:
    import atproto  # noqa: F401
    from atproto_client.exceptions import RequestException
except ModuleNotFoundError:
    # The shared CI test environment intentionally installs only the service
    # dependencies used by most tests. Keep these unit tests independent of the
    # real SDK while matching the exception interface used by the producer.
    class _RequestErrorBase(Exception):
        def __init__(self, response=None):
            super().__init__()
            self.response = response

    class RequestException(_RequestErrorBase):
        pass

    atproto_module = ModuleType("atproto")
    atproto_module.AsyncClient = type("AsyncClient", (), {})
    exceptions_module = ModuleType("atproto_client.exceptions")
    exceptions_module.RequestErrorBase = _RequestErrorBase
    exceptions_module.RequestException = RequestException
    atproto_client_module = ModuleType("atproto_client")
    atproto_client_module.exceptions = exceptions_module

    sys.modules["atproto"] = atproto_module
    sys.modules["atproto_client"] = atproto_client_module
    sys.modules["atproto_client.exceptions"] = exceptions_module


bluesky = importlib.import_module("bluesky-producer.main")


class _ExpectedInteger(BaseModel):
    value: int


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as error:
        _ExpectedInteger(value="not-an-integer")
    return error.value


def _client_with_search_results(*results):
    client = MagicMock()
    client.app.bsky.feed.search_posts = AsyncMock(side_effect=results)
    return client


@pytest.mark.asyncio
async def test_validation_error_skips_only_affected_search_term(monkeypatch):
    client = _client_with_search_results(
        _validation_error(),
        SimpleNamespace(posts=[]),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(bluesky.asyncio, "sleep", sleep)

    rate_limited = await bluesky.poll_search_terms(
        client,
        MagicMock(),
        {"PLTR": ["Alex Karp", "Palantir"]},
        {},
    )

    assert rate_limited is False
    assert client.app.bsky.feed.search_posts.await_count == 2
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_successful_search_publishes_and_advances_last_seen(monkeypatch):
    post = SimpleNamespace(
        cid="post-cid",
        record=SimpleNamespace(
            created_at="2026-07-21T20:00:00Z",
            text="Palantir earnings",
        ),
        like_count=2,
        repost_count=1,
        reply_count=1,
    )
    client = _client_with_search_results(SimpleNamespace(posts=[post]))
    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock()
    monkeypatch.setattr(bluesky, "match_symbol", lambda text, symbol: True)
    last_seen = {}

    published = await bluesky.search_and_publish(
        client,
        channel,
        "PLTR",
        "Palantir",
        last_seen,
    )

    assert published == 1
    channel.default_exchange.publish.assert_awaited_once()
    assert last_seen == {"Palantir": "2026-07-21T20:00:00Z"}


@pytest.mark.asyncio
async def test_http_429_stops_batch_and_signals_rate_limit(monkeypatch):
    response = SimpleNamespace(status_code=429)
    client = _client_with_search_results(RequestException(response))
    sleep = AsyncMock()
    monkeypatch.setattr(bluesky.asyncio, "sleep", sleep)

    rate_limited = await bluesky.poll_search_terms(
        client,
        MagicMock(),
        {"PLTR": ["Alex Karp", "Palantir"]},
        {},
    )

    assert rate_limited is True
    client.app.bsky.feed.search_posts.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_rate_request_error_continues_to_next_term(monkeypatch):
    response = SimpleNamespace(status_code=503)
    client = _client_with_search_results(
        RequestException(response),
        SimpleNamespace(posts=[]),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(bluesky.asyncio, "sleep", sleep)

    rate_limited = await bluesky.poll_search_terms(
        client,
        MagicMock(),
        {"PLTR": ["Alex Karp", "Palantir"]},
        {},
    )

    assert rate_limited is False
    assert client.app.bsky.feed.search_posts.await_count == 2
    assert sleep.await_count == 2
