"""Process-local tracked-symbol cache with an explicit async lifecycle."""

import asyncio
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypedDict

import psycopg

from shared.config import DATABASE_DSN


logger = logging.getLogger("shared-symbols")

DEFAULT_REFRESH_INTERVAL_SECONDS = 60.0
DEFAULT_RETRY_BASE_SECONDS = 5.0
DEFAULT_RETRY_MAX_SECONDS = 60.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5


class SymbolConfig(TypedDict):
    keywords: list[str]
    future: str | None
    sector: str | None
    require_uppercase: bool
    block_phrases: list[str]
    require_cashtag: bool


def _decode_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]

    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        raise ValueError("Expected a JSON list")
    return [str(item) for item in decoded]


class SymbolRegistry:
    """Thread-safe snapshot refreshed by one lifecycle-owned asyncio task."""

    def __init__(
        self,
        dsn: str = DATABASE_DSN,
        *,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        retry_base: float = DEFAULT_RETRY_BASE_SECONDS,
        retry_max: float = DEFAULT_RETRY_MAX_SECONDS,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        if refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive")
        if retry_base <= 0:
            raise ValueError("retry_base must be positive")
        if retry_max < retry_base:
            raise ValueError("retry_max must be greater than or equal to retry_base")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        self._dsn = dsn
        self._refresh_interval = refresh_interval
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._connect_timeout = connect_timeout
        self._symbols: dict[str, SymbolConfig] = {}
        self._snapshot_lock = threading.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    def refresh(self) -> bool:
        """Load and atomically publish a snapshot, retaining the old one on error."""
        try:
            with psycopg.connect(
                self._dsn,
                connect_timeout=self._connect_timeout,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            symbol,
                            keywords,
                            future,
                            sector,
                            require_uppercase,
                            block_phrases,
                            require_cashtag
                        FROM tracked_symbols
                        WHERE is_active = true
                        """
                    )
                    rows = cur.fetchall()

            new_symbols = {
                row[0]: SymbolConfig(
                    keywords=_decode_string_list(row[1]),
                    future=row[2],
                    sector=row[3],
                    require_uppercase=row[4],
                    block_phrases=_decode_string_list(row[5]),
                    require_cashtag=row[6],
                )
                for row in rows
            }

            # Empty is a valid successful snapshot: all tracked symbols may have
            # intentionally been disabled. Replacing it prevents stale polling.
            with self._snapshot_lock:
                self._symbols = new_symbols
            logger.debug("Refreshed %d active symbols", len(new_symbols))
            return True
        except Exception:
            logger.exception(
                "Failed to refresh tracked symbols; retaining last known snapshot"
            )
            return False

    async def start(self) -> None:
        """Perform the initial load and start the managed refresh task."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        initial_refresh_succeeded = await asyncio.to_thread(self.refresh)
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(initial_refresh_succeeded),
            name="tracked-symbol-refresh",
        )

    async def stop(self) -> None:
        """Cancel and await the refresh task; safe to call more than once."""
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _refresh_loop(self, initial_refresh_succeeded: bool) -> None:
        retry_delay = self._retry_base
        if initial_refresh_succeeded:
            next_delay = self._refresh_interval
        else:
            next_delay = retry_delay
            retry_delay = min(retry_delay * 2, self._retry_max)

        while True:
            await asyncio.sleep(next_delay)
            refresh_succeeded = await asyncio.to_thread(self.refresh)
            if refresh_succeeded:
                next_delay = self._refresh_interval
                retry_delay = self._retry_base
            else:
                next_delay = retry_delay
                retry_delay = min(retry_delay * 2, self._retry_max)

    def tickers(self) -> list[str]:
        with self._snapshot_lock:
            return sorted(self._symbols)

    def keywords_map(self) -> dict[str, list[str]]:
        with self._snapshot_lock:
            return {
                ticker: [ticker, f"${ticker}", *config["keywords"]]
                for ticker, config in self._symbols.items()
            }

    def primary_futures_map(self) -> dict[str, str | None]:
        with self._snapshot_lock:
            return {
                ticker: config["future"]
                for ticker, config in self._symbols.items()
            }

    def sector_map(self) -> dict[str, str | None]:
        with self._snapshot_lock:
            return {
                ticker: config["sector"]
                for ticker, config in self._symbols.items()
            }

    def match_symbol(self, text: str, symbol: str) -> bool:
        with self._snapshot_lock:
            config = self._symbols.get(symbol)
        if config is None:
            return False

        lowered_text = text.lower()
        for phrase in config["block_phrases"]:
            if phrase.lower() in lowered_text:
                return False

        cashtag = f"${symbol}"
        if re.search(
            rf"(?<![a-zA-Z0-9]){re.escape(cashtag)}(?![a-zA-Z0-9])",
            text,
            re.IGNORECASE,
        ):
            return True

        for keyword in config["keywords"]:
            if re.search(
                rf"(?<![a-zA-Z0-9]){re.escape(keyword)}(?![a-zA-Z0-9])",
                text,
                re.IGNORECASE,
            ):
                return True

        if config["require_cashtag"]:
            return False

        flags = 0 if config["require_uppercase"] else re.IGNORECASE
        pattern = rf"(?<![a-zA-Z0-9]){re.escape(symbol)}(?![a-zA-Z0-9])"
        return re.search(pattern, text, flags) is not None


_registry = SymbolRegistry()


async def start_symbol_registry() -> None:
    await _registry.start()


async def stop_symbol_registry() -> None:
    await _registry.stop()


async def run_with_symbol_registry(
    main: Callable[[], Awaitable[None]],
) -> None:
    """Run a service with symbol refresh active for exactly its lifetime."""
    await start_symbol_registry()
    try:
        await main()
    finally:
        await stop_symbol_registry()


def tickers() -> list[str]:
    return _registry.tickers()


def keywords_map() -> dict[str, list[str]]:
    return _registry.keywords_map()


def primary_futures_map() -> dict[str, str | None]:
    return _registry.primary_futures_map()


def sector_map() -> dict[str, str | None]:
    return _registry.sector_map()


def match_symbol(text: str, symbol: str) -> bool:
    return _registry.match_symbol(text, symbol)
