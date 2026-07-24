import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

api_main = importlib.import_module("api-service.main")
context_api = importlib.import_module("api-service.global_context_api")


class FakeCursor:
    def __init__(self, result_sets):
        self.result_sets = iter(result_sets)
        self.current = []
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, params):
        self.executions.append((query, params))
        self.current = next(self.result_sets)

    async def fetchall(self):
        return self.current


class FakeConnection:
    def __init__(self, result_sets):
        self.cursor_instance = FakeCursor(result_sets)

    def cursor(self):
        return self.cursor_instance


class AsyncContext:
    def __init__(self, value):
        self.value = value
        self.exited_with = None

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, *_args):
        self.exited_with = exc_type
        return False


class AdminCursor:
    def __init__(self, *, exists=True, fail_executemany=False):
        self.exists = exists
        self.fail_executemany = fail_executemany
        self.executions = []
        self.executemany_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, params):
        self.executions.append((query, params))

    async def executemany(self, query, params):
        if self.fail_executemany:
            raise RuntimeError("insert failed")
        self.executemany_calls.append((query, params))

    async def fetchone(self):
        return {"exists": self.exists}


class AdminConnection:
    def __init__(self, **cursor_options):
        self.cursor_instance = AdminCursor(**cursor_options)
        self.transaction_context = AsyncContext(None)

    def cursor(self):
        return self.cursor_instance

    def transaction(self):
        return self.transaction_context


def _history_rows(instrument_key, start, returns):
    price = 100.0
    rows = [
        {
            "instrument_key": instrument_key,
            "ends_at": start,
            "close_price": price,
            "fetched_at": start + timedelta(minutes=5),
        }
    ]
    for index, value in enumerate(returns, start=1):
        price *= 1 + value
        end = start + timedelta(days=index)
        rows.append(
            {
                "instrument_key": instrument_key,
                "ends_at": end,
                "close_price": price,
                "fetched_at": end + timedelta(minutes=5),
            }
        )
    return rows


def test_replacement_models_reject_duplicates_and_empty_rules():
    with pytest.raises(ValidationError, match="must be unique"):
        context_api.ExposureReplacement(
            exposures=[
                {
                    "instrument_key": "index:nikkei-225",
                    "reason": "first",
                },
                {
                    "instrument_key": "index:nikkei-225",
                    "reason": "duplicate",
                },
            ]
        )
    with pytest.raises(ValidationError, match="at least one"):
        context_api.EventRuleInput(name="empty")


def test_global_context_feature_flag_fails_closed(monkeypatch):
    monkeypatch.setattr(api_main, "GLOBAL_CONTEXT_ENABLED", False)
    with pytest.raises(HTTPException) as raised:
        api_main.require_global_context_enabled()
    assert raised.value.status_code == 404

    monkeypatch.setattr(api_main, "GLOBAL_CONTEXT_ENABLED", True)
    api_main.require_global_context_enabled()


@pytest.mark.asyncio
async def test_admin_exposure_replacement_is_one_transaction(monkeypatch):
    connection = AdminConnection()
    connection.transaction_context.value = connection
    connection_context = AsyncContext(connection)
    monkeypatch.setattr(api_main, "GLOBAL_CONTEXT_ENABLED", True)
    monkeypatch.setattr(
        api_main,
        "get_db_conn",
        lambda: connection_context,
    )
    replacement = context_api.ExposureReplacement(
        exposures=[
            {
                "instrument_key": "index:nikkei-225",
                "reason": "Asia session",
                "display_order": 1,
            }
        ]
    )

    response = await api_main.replace_global_context_exposures(
        "nvda",
        replacement,
        api_key="test",
    )

    assert response == {"status": "success", "replaced": 1}
    statements = [
        " ".join(query.lower().split())
        for query, _params in connection.cursor_instance.executions
    ]
    assert any(
        "delete from stock_factor_exposures where symbol = %s" in statement
        for statement in statements
    )
    assert len(connection.cursor_instance.executemany_calls) == 1
    assert connection.transaction_context.exited_with is None


@pytest.mark.asyncio
async def test_admin_replacement_propagates_failure_for_transaction_rollback(
    monkeypatch,
):
    connection = AdminConnection(fail_executemany=True)
    connection.transaction_context.value = connection
    monkeypatch.setattr(api_main, "GLOBAL_CONTEXT_ENABLED", True)
    monkeypatch.setattr(
        api_main,
        "get_db_conn",
        lambda: AsyncContext(connection),
    )
    replacement = context_api.ExposureReplacement(
        exposures=[
            {
                "instrument_key": "index:nikkei-225",
                "reason": "Asia session",
            }
        ]
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        await api_main.replace_global_context_exposures(
            "NVDA",
            replacement,
            api_key="test",
        )

    assert connection.transaction_context.exited_with is RuntimeError


@pytest.mark.asyncio
async def test_unconfigured_symbol_returns_typed_empty_context():
    connection = FakeConnection([[]])

    response = await context_api.build_global_context(
        connection,
        symbol="NVDA",
        horizon_sessions=30,
    )

    assert response.configured is False
    assert response.factors == []
    assert response.events == []
    assert response.freshness.status == "empty"
    assert len(connection.cursor_instance.executions) == 1


@pytest.mark.asyncio
async def test_global_context_route_accepts_string_encoded_integer_horizon(
    monkeypatch,
):
    observed = {}

    async def fake_build_global_context(
        _connection,
        *,
        symbol,
        horizon_sessions,
    ):
        observed["symbol"] = symbol
        observed["horizon_sessions"] = horizon_sessions
        return context_api.GlobalContextResponse(
            symbol=symbol,
            configured=False,
            horizon_sessions=horizon_sessions,
            as_of=datetime.now(timezone.utc),
            currency_orientation="test",
            disclaimer="test",
            factors=[],
            events=[],
            freshness=context_api.GlobalFreshnessResponse(
                latest_factor_at=None,
                latest_daily_at=None,
                latest_event_at=None,
                status="empty",
            ),
        )

    monkeypatch.setattr(api_main, "GLOBAL_CONTEXT_ENABLED", True)
    monkeypatch.setattr(
        api_main,
        "get_db_conn",
        lambda: AsyncContext(object()),
    )
    monkeypatch.setattr(
        api_main,
        "build_global_context",
        fake_build_global_context,
    )
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/stats/global-context",
            params={"symbol": "nvda", "horizon_sessions": "30"},
        )
        invalid = await client.get(
            "/api/stats/global-context",
            params={"symbol": "NVDA", "horizon_sessions": "45"},
        )

    assert response.status_code == 200
    assert observed == {"symbol": "NVDA", "horizon_sessions": 30}
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_context_response_includes_relationship_event_reaction_and_freshness(
    monkeypatch,
):
    start = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    factor_returns = [
        0.011,
        -0.006,
        0.018,
        0.004,
        -0.014,
    ] * 6
    stock_returns = [0.003, *factor_returns]
    history = [
        *_history_rows("index:nikkei-225", start, factor_returns),
        *_history_rows(
            "us-stock:NVDA",
            start.replace(hour=21),
            stock_returns,
        ),
    ]
    latest = history[30]["ends_at"]
    factor_rows = [
        {
            "instrument_key": "index:nikkei-225",
            "exposure_reason": "Asia semiconductor session",
            "display_name": "Nikkei 225",
            "asset_class": "index",
            "currency": "JPY",
            "exchange": "JPX",
            "timezone": "Asia/Tokyo",
            "quote_convention": None,
            "current_price": 39000.0,
            "reference_price": 38600.0,
            "current_as_of": latest,
            "fetched_at": latest,
        }
    ]
    event_time = start.replace(hour=12) + timedelta(days=10)
    event_rows = [
        {
            "id": 10,
            "title": "Recorded event",
            "summary": None,
            "canonical_url": "https://example.com/event",
            "source_name": "example.com",
            "occurred_at": event_time,
            "provider": "gdelt",
            "ingested_at": event_time + timedelta(minutes=5),
            "rule_names": ["Asia supply"],
            "match_reasons": [{"rule_name": "Asia supply"}],
        }
    ]
    metric = MagicMock()
    monkeypatch.setattr(
        context_api,
        "GLOBAL_RELATIONSHIP_SAMPLE_SUFFICIENT",
        metric,
    )
    connection = FakeConnection([factor_rows, history, event_rows])

    response = await context_api.build_global_context(
        connection,
        symbol="NVDA",
        horizon_sessions=30,
    )

    assert response.configured is True
    assert response.factors[0].current_move_pct == pytest.approx(
        (39000 / 38600 - 1) * 100
    )
    assert response.factors[0].relationship.sample_count >= 20
    assert response.events[0].reaction_label == "next-close move"
    assert response.events[0].next_close_move_pct is not None
    assert response.freshness.latest_daily_at is not None
    metric.labels.assert_called_once_with(
        symbol="NVDA",
        instrument_key="index:nikkei-225",
        horizon_sessions="30",
    )
    metric.labels.return_value.set.assert_called_once()


def test_bar_upsert_contract_is_idempotent():
    db_module = importlib.import_module("db")
    sql = " ".join(db_module.UPSERT_GLOBAL_BAR_SQL.lower().split())

    assert "on conflict (instrument_key, interval, starts_at) do update" in sql
    assert "provider = excluded.provider" in sql
