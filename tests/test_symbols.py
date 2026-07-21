import asyncio
import importlib
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

import shared.symbols as symbols


def _database(monkeypatch, rows):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchall.return_value = rows
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(symbols.psycopg, "connect", connect)
    return connect, cursor


def _symbol_row(
    symbol="NVDA",
    *,
    keywords=None,
    future="NQ=F",
    sector="Technology",
    require_uppercase=False,
    block_phrases=None,
    require_cashtag=False,
):
    return (
        symbol,
        ["nvidia"] if keywords is None else keywords,
        future,
        sector,
        require_uppercase,
        [] if block_phrases is None else block_phrases,
        require_cashtag,
    )


def test_import_does_not_connect_or_start_thread():
    with (
        patch.object(symbols.psycopg, "connect") as connect,
        patch.object(threading.Thread, "start") as start_thread,
    ):
        reloaded = importlib.reload(symbols)

    connect.assert_not_called()
    start_thread.assert_not_called()
    assert reloaded._registry._refresh_task is None


def test_refresh_publishes_parsed_snapshot(monkeypatch):
    rows = [
        _symbol_row(block_phrases='["nvidia shield"]'),
        _symbol_row(
            "SMH",
            keywords='["semiconductor ETF"]',
            future=None,
            require_uppercase=True,
            require_cashtag=True,
        ),
    ]
    connect, _ = _database(monkeypatch, rows)
    registry = symbols.SymbolRegistry("mock_dsn", connect_timeout=3)

    assert registry.refresh() is True

    connect.assert_called_once_with("mock_dsn", connect_timeout=3)
    assert registry.tickers() == ["NVDA", "SMH"]
    assert registry.keywords_map()["NVDA"] == ["NVDA", "$NVDA", "nvidia"]
    assert registry.primary_futures_map() == {"NVDA": "NQ=F", "SMH": None}
    assert registry.sector_map()["NVDA"] == "Technology"
    assert registry.match_symbol("Nvidia reported earnings", "NVDA") is True
    assert registry.match_symbol("New NVIDIA Shield released", "NVDA") is False
    assert registry.match_symbol("SMH rallied", "SMH") is False
    assert registry.match_symbol("$SMH rallied", "SMH") is True


def test_successful_empty_refresh_clears_stale_symbols(monkeypatch):
    _, cursor = _database(monkeypatch, [_symbol_row()])
    cursor.fetchall.side_effect = [[_symbol_row()], []]
    registry = symbols.SymbolRegistry("mock_dsn")

    assert registry.refresh() is True
    assert registry.tickers() == ["NVDA"]

    assert registry.refresh() is True
    assert registry.tickers() == []


def test_failed_refresh_retains_last_known_snapshot(monkeypatch):
    connect, _ = _database(monkeypatch, [_symbol_row()])
    registry = symbols.SymbolRegistry("mock_dsn")
    assert registry.refresh() is True

    connect.side_effect = psycopg.OperationalError("database unavailable")

    assert registry.refresh() is False
    assert registry.tickers() == ["NVDA"]


@pytest.mark.asyncio
async def test_start_and_stop_manage_exactly_one_refresh_task(monkeypatch):
    async def to_thread_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(symbols.asyncio, "to_thread", to_thread_inline)
    registry = symbols.SymbolRegistry("mock_dsn", refresh_interval=3600)
    registry.refresh = MagicMock(return_value=True)

    await registry.start()
    refresh_task = registry._refresh_task
    await registry.start()

    assert registry.refresh.call_count == 1
    assert refresh_task is registry._refresh_task
    assert refresh_task is not None

    await registry.stop()
    await registry.stop()

    assert refresh_task.cancelled()
    assert registry._refresh_task is None


@pytest.mark.asyncio
async def test_refresh_loop_uses_bounded_backoff_and_resets_after_success(
    monkeypatch,
):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    async def to_thread_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(symbols.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(symbols.asyncio, "to_thread", to_thread_inline)
    registry = symbols.SymbolRegistry(
        "mock_dsn",
        refresh_interval=60,
        retry_base=5,
        retry_max=10,
    )
    registry.refresh = MagicMock(side_effect=[False, False, True])

    with pytest.raises(asyncio.CancelledError):
        await registry._refresh_loop(initial_refresh_succeeded=False)

    assert delays == [5, 10, 10, 60]


@pytest.mark.asyncio
async def test_service_wrapper_stops_registry_when_main_fails(monkeypatch):
    calls = []

    async def failing_main():
        calls.append("main")
        raise RuntimeError("service failed")

    start = AsyncMock(side_effect=lambda: calls.append("start"))
    stop = AsyncMock(side_effect=lambda: calls.append("stop"))
    monkeypatch.setattr(symbols, "start_symbol_registry", start)
    monkeypatch.setattr(symbols, "stop_symbol_registry", stop)

    with pytest.raises(RuntimeError, match="service failed"):
        await symbols.run_with_symbol_registry(failing_main)

    assert calls == ["start", "main", "stop"]


def test_all_polled_futures_reads_current_symbol_snapshot(monkeypatch):
    monkeypatch.setitem(sys.modules, "pandas_market_calendars", MagicMock())
    monkeypatch.delitem(sys.modules, "shared.futures", raising=False)
    futures = importlib.import_module("shared.futures")
    primary_map = MagicMock(
        side_effect=[
            {"NVDA": "NQ=F"},
            {"NVDA": "NQ=F", "IWM": "RTY=F"},
        ]
    )
    monkeypatch.setattr(futures, "primary_futures_map", primary_map)

    assert futures.all_polled_futures() == ["NQ=F", "^VIX"]
    assert futures.all_polled_futures() == ["NQ=F", "RTY=F", "^VIX"]


@pytest.mark.asyncio
async def test_api_lifespan_starts_and_stops_symbol_registry():
    api_main = importlib.import_module("api-service.main")
    pool = MagicMock()
    pool.open = AsyncMock()
    pool.close = AsyncMock()

    with (
        patch.object(api_main, "start_symbol_registry", new_callable=AsyncMock) as start,
        patch.object(api_main, "stop_symbol_registry", new_callable=AsyncMock) as stop,
        patch.object(api_main, "postgres_listener", new_callable=AsyncMock),
        patch.object(api_main, "AsyncConnectionPool", return_value=pool),
    ):
        async with api_main.lifespan(api_main.app):
            start.assert_awaited_once_with()
            pool.open.assert_awaited_once_with()

    stop.assert_awaited_once_with()
    pool.close.assert_awaited_once_with()
