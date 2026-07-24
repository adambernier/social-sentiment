import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

adapter_module = importlib.import_module("global-events-producer.adapter")

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gdelt_articles.json").read_text()
)


def _rule():
    return adapter_module.EventRule(
        id=7,
        symbol="NVDA",
        name="Asia semiconductor supply",
        countries=("Taiwan", "South Korea"),
        themes=("SUPPLY_CHAIN",),
        query_terms=("semiconductor", "advanced packaging"),
    )


def test_gdelt_query_is_deterministic_and_rule_bound():
    query = adapter_module.build_gdelt_query(_rule())

    assert query == (
        '("semiconductor" OR "advanced packaging") AND '
        '("Taiwan" OR "South Korea") AND (theme:"SUPPLY_CHAIN")'
    )


def test_gdelt_query_sanitizes_provider_control_characters():
    rule = adapter_module.EventRule(
        id=1,
        symbol="NVDA",
        name="safe",
        countries=(),
        themes=(),
        query_terms=('chip") OR *',),
    )

    query = adapter_module.build_gdelt_query(rule)

    assert "*" not in query
    assert query.count('"') == 2


@pytest.mark.asyncio
async def test_recorded_fixture_normalizes_and_deduplicates_articles():
    async def handler(request):
        assert request.url.params["format"] == "json"
        assert request.url.params["maxrecords"] == "20"
        return httpx.Response(200, json=FIXTURE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        events = await adapter_module.GdeltEventAdapter(client).fetch(
            _rule(),
            since=datetime(2026, 7, 21, tzinfo=timezone.utc),
            max_results=20,
        )

    assert len(events) == 2
    assert len({event.provider_event_id for event in events}) == 2
    assert events[0].occurred_at == datetime(
        2026,
        7,
        23,
        4,
        15,
        tzinfo=timezone.utc,
    )
    assert events[0].countries == ("Taiwan", "South Korea")
    assert events[0].themes == ("SUPPLY_CHAIN",)


@pytest.mark.asyncio
async def test_gdelt_rate_limit_keeps_retry_after():
    async def handler(_request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(adapter_module.EventProviderRateLimited) as raised:
            await adapter_module.GdeltEventAdapter(client).fetch(
                _rule(),
                since=datetime(2026, 7, 21, tzinfo=timezone.utc),
                max_results=20,
            )

    assert raised.value.retry_after_seconds == 120


def test_empty_rule_is_rejected():
    empty = adapter_module.EventRule(
        id=1,
        symbol="NVDA",
        name="empty",
        countries=(),
        themes=(),
        query_terms=(),
    )
    with pytest.raises(ValueError, match="must contain"):
        adapter_module.build_gdelt_query(empty)
