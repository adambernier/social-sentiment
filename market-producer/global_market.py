"""Provider-neutral global-market ingestion with a Yahoo pilot adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as wall_time
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

from shared.config import get_env_int
from shared.global_context import (
    BarInterval,
    InstrumentMetadata,
    NormalizedMarketBar,
)
from shared.metrics import (
    GLOBAL_LAST_SUCCESS_TIMESTAMP,
    GLOBAL_PROVIDER_REQUESTS_TOTAL,
    RATE_LIMITS_HIT_TOTAL,
)
from shared.pacing import AsyncRateLimiter, PerSymbolBackoff, paced_gather
from shared.polling import PollStatus

logger = logging.getLogger("global-market")

GLOBAL_MARKET_POLL_SECONDS = get_env_int(
    "GLOBAL_MARKET_POLL_SECONDS",
    15 * 60,
)
GLOBAL_MARKET_MAX_CONCURRENCY = get_env_int(
    "GLOBAL_MARKET_MAX_CONCURRENCY",
    2,
)
GLOBAL_MARKET_RATE_PER_MINUTE = get_env_int(
    "GLOBAL_MARKET_RATE_PER_MINUTE",
    20,
)
GLOBAL_MARKET_MAX_BACKOFF = get_env_int(
    "GLOBAL_MARKET_MAX_BACKOFF",
    4 * 3600,
)
GLOBAL_DAILY_SETTLE_GRACE_MINUTES = get_env_int(
    "GLOBAL_DAILY_SETTLE_GRACE_MINUTES",
    30,
)


@dataclass(frozen=True)
class ProviderMetadata:
    provider_symbol: str
    currency: str | None
    exchange: str | None
    timezone: str | None


class MarketDataAdapter(Protocol):
    provider_name: str

    def get_metadata(self, provider_symbol: str) -> ProviderMetadata:
        """Return provider metadata without exposing it as instrument identity."""

    def fetch_bars(
        self,
        instrument: InstrumentMetadata,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> list[NormalizedMarketBar]:
        """Return UTC-normalized, provider-neutral OHLC bars."""


def normalize_fx_ohlc(
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    *,
    provider_is_local_per_usd: bool,
) -> tuple[float, float, float, float]:
    """Normalize FX so positive returns always mean local-currency weakness."""
    if provider_is_local_per_usd:
        return open_price, high_price, low_price, close_price
    if min(open_price, high_price, low_price, close_price) <= 0:
        raise ValueError("FX prices must be positive before inversion")
    return (
        1.0 / open_price,
        1.0 / low_price,
        1.0 / high_price,
        1.0 / close_price,
    )


def _parse_session_time(value: object, fallback: wall_time) -> wall_time:
    if not isinstance(value, str):
        return fallback
    try:
        hour, minute = value.split(":", maxsplit=1)
        return wall_time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError):
        return fallback


def _daily_bounds(
    session_date: date,
    timezone_name: str,
    session_metadata: dict[str, object],
) -> tuple[datetime, datetime]:
    local_timezone = ZoneInfo(timezone_name)
    open_time = _parse_session_time(
        session_metadata.get("open"),
        wall_time(0, 0),
    )
    close_time = _parse_session_time(
        session_metadata.get("close"),
        wall_time(23, 59),
    )
    start_date = session_date
    if open_time > close_time:
        start_date -= timedelta(days=1)
    starts_at = datetime.combine(
        start_date,
        open_time,
        tzinfo=local_timezone,
    )
    ends_at = datetime.combine(
        session_date,
        close_time,
        tzinfo=local_timezone,
    )
    return starts_at.astimezone(timezone.utc), ends_at.astimezone(timezone.utc)


class YahooMarketDataAdapter:
    """Yahoo/yfinance implementation isolated behind the adapter contract."""

    provider_name = "yahoo"

    def get_metadata(self, provider_symbol: str) -> ProviderMetadata:
        ticker = yf.Ticker(provider_symbol)
        info = ticker.info
        return ProviderMetadata(
            provider_symbol=provider_symbol,
            currency=info.get("currency"),
            exchange=info.get("exchange"),
            timezone=info.get("exchangeTimezoneName"),
        )

    def fetch_bars(
        self,
        instrument: InstrumentMetadata,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> list[NormalizedMarketBar]:
        provider_interval = "1h" if interval == "1h" else "1d"
        frame = yf.Ticker(instrument.provider_alias).history(
            start=start,
            end=end,
            interval=provider_interval,
            auto_adjust=False,
            actions=False,
        )
        if frame.empty:
            return []
        return self._normalize_frame(instrument, interval, frame)

    def _normalize_frame(
        self,
        instrument: InstrumentMetadata,
        interval: BarInterval,
        frame: pd.DataFrame,
    ) -> list[NormalizedMarketBar]:
        output: list[NormalizedMarketBar] = []
        local_timezone = ZoneInfo(instrument.timezone)

        for index, row in frame.iterrows():
            values = (
                row.get("Open"),
                row.get("High"),
                row.get("Low"),
                row.get("Close"),
            )
            if any(
                value is None or not math.isfinite(float(value)) for value in values
            ):
                continue
            open_price, high_price, low_price, close_price = map(float, values)

            if (
                instrument.asset_class == "fx"
                and instrument.quote_convention == "local_currency_per_usd"
            ):
                (
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                ) = normalize_fx_ohlc(
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    provider_is_local_per_usd=True,
                )

            timestamp = pd.Timestamp(index)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(local_timezone)
            local_timestamp = timestamp.to_pydatetime().astimezone(local_timezone)
            bar_session_date = local_timestamp.date()
            if interval == "1d":
                starts_at, ends_at = _daily_bounds(
                    bar_session_date,
                    instrument.timezone,
                    instrument.session_metadata,
                )
            else:
                starts_at = local_timestamp.astimezone(timezone.utc)
                ends_at = starts_at + timedelta(hours=1)

            raw_volume = row.get("Volume")
            volume = None
            if raw_volume is not None and math.isfinite(float(raw_volume)):
                volume = max(float(raw_volume), 0.0)

            output.append(
                NormalizedMarketBar(
                    instrument_key=instrument.instrument_key,
                    interval=interval,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    session_date=bar_session_date,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=volume,
                    provider=self.provider_name,
                )
            )
        return output


def instrument_from_row(row: dict) -> InstrumentMetadata | None:
    aliases = row.get("provider_aliases") or {}
    provider_alias = aliases.get("yahoo")
    if not provider_alias:
        return None
    return InstrumentMetadata(
        instrument_key=row["instrument_key"],
        display_name=row["display_name"],
        asset_class=row["asset_class"],
        currency=row["currency"],
        exchange=row.get("exchange"),
        timezone=row["timezone"],
        provider_alias=provider_alias,
        session_metadata=row.get("session_metadata") or {},
        quote_convention=row.get("quote_convention"),
    )


class GlobalContextMarketRunner:
    """Runs separately paced context polling inside market-producer."""

    def __init__(
        self,
        db,
        adapter: MarketDataAdapter,
        tracked_symbols,
    ):
        self.db = db
        self.adapter = adapter
        self.tracked_symbols = tracked_symbols
        self.limiter = AsyncRateLimiter(
            max_rate=GLOBAL_MARKET_RATE_PER_MINUTE,
            period=60.0,
        )
        self.backoff = PerSymbolBackoff(
            base_interval=GLOBAL_MARKET_POLL_SECONDS,
            max_backoff=GLOBAL_MARKET_MAX_BACKOFF,
        )
        self.last_hourly_poll = 0.0
        self.last_daily_session: dict[str, date] = {}

    def _load_instruments(self) -> list[InstrumentMetadata]:
        symbols = list(self.tracked_symbols())
        self.db.ensure_us_equity_instruments(symbols)
        instruments = (
            instrument_from_row(row) for row in self.db.list_global_instruments()
        )
        return [instrument for instrument in instruments if instrument is not None]

    @staticmethod
    def _daily_is_due(
        instrument: InstrumentMetadata,
        now_utc: datetime,
        last_session: date | None,
    ) -> bool:
        local_now = now_utc.astimezone(ZoneInfo(instrument.timezone))
        configured_weekdays = instrument.session_metadata.get(
            "weekdays",
            [1, 2, 3, 4, 5],
        )
        weekdays = (
            configured_weekdays
            if isinstance(configured_weekdays, (list, tuple, set))
            else [1, 2, 3, 4, 5]
        )
        if local_now.isoweekday() not in weekdays:
            return False
        close_time = _parse_session_time(
            instrument.session_metadata.get("close"),
            wall_time(23, 59),
        )
        settled_at = datetime.combine(
            local_now.date(),
            close_time,
            tzinfo=local_now.tzinfo,
        ) + timedelta(minutes=GLOBAL_DAILY_SETTLE_GRACE_MINUTES)
        return local_now >= settled_at and last_session != local_now.date()

    def _fetch_one(
        self,
        request: tuple[InstrumentMetadata, BarInterval, datetime, datetime],
    ) -> tuple[list[NormalizedMarketBar], PollStatus]:
        instrument, interval, start, end = request
        try:
            bars = self.adapter.fetch_bars(
                instrument,
                interval,
                start,
                end,
            )
            if not bars:
                return [], PollStatus.NO_DATA
            return bars, PollStatus.SUCCESS
        except YFRateLimitError:
            return [], PollStatus.RATE_LIMITED
        except Exception:
            logger.exception(
                "Global %s fetch failed for %s",
                interval,
                instrument.instrument_key,
            )
            return [], PollStatus.TRANSIENT_ERROR

    async def _poll(
        self,
        instruments: list[InstrumentMetadata],
        interval: BarInterval,
        now_utc: datetime,
    ) -> set[str]:
        if not instruments:
            return set()
        lookback = timedelta(days=3 if interval == "1h" else 10)
        requests = [
            (instrument, interval, now_utc - lookback, now_utc + timedelta(days=1))
            for instrument in instruments
        ]
        due_keys = set(
            self.backoff.due(
                [
                    f"{instrument.instrument_key}:{interval}"
                    for instrument in instruments
                ]
            )
        )
        due_requests = [
            request
            for request in requests
            if f"{request[0].instrument_key}:{interval}" in due_keys
        ]
        results = await paced_gather(
            due_requests,
            lambda request: asyncio.to_thread(self._fetch_one, request),
            max_concurrency=GLOBAL_MARKET_MAX_CONCURRENCY,
            limiter=self.limiter,
        )

        completed: set[str] = set()
        for request, (bars, status) in zip(due_requests, results):
            instrument = request[0]
            request_key = f"{instrument.instrument_key}:{interval}"
            rate_limited = status is PollStatus.RATE_LIMITED
            self.backoff.record(request_key, rate_limited)
            GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
                provider=self.adapter.provider_name,
                data_type=f"bars_{interval}",
                status=status.value,
            ).inc()
            if rate_limited:
                RATE_LIMITS_HIT_TOTAL.labels(platform="global-market").inc()
                continue
            if not bars:
                if status is PollStatus.NO_DATA:
                    completed.add(instrument.instrument_key)
                continue
            await asyncio.to_thread(self.db.upsert_global_bars, bars)
            completed.add(instrument.instrument_key)
            latest = max(bar.ends_at for bar in bars)
            GLOBAL_LAST_SUCCESS_TIMESTAMP.labels(
                provider=self.adapter.provider_name,
                data_type=f"bars_{interval}",
            ).set(latest.timestamp())
        return completed

    async def maybe_run(self, now_utc: datetime) -> None:
        """Poll due intervals without allowing failures to stop core quotes."""
        try:
            instruments = await asyncio.to_thread(self._load_instruments)
            monotonic_now = time.monotonic()
            if monotonic_now - self.last_hourly_poll >= GLOBAL_MARKET_POLL_SECONDS:
                hourly = [
                    instrument
                    for instrument in instruments
                    if instrument.asset_class != "us_equity"
                ]
                await self._poll(hourly, "1h", now_utc)
                self.last_hourly_poll = monotonic_now

            daily = [
                instrument
                for instrument in instruments
                if self._daily_is_due(
                    instrument,
                    now_utc,
                    self.last_daily_session.get(instrument.instrument_key),
                )
            ]
            completed_daily = await self._poll(daily, "1d", now_utc)
            for instrument in daily:
                if instrument.instrument_key not in completed_daily:
                    continue
                local_date = now_utc.astimezone(ZoneInfo(instrument.timezone)).date()
                self.last_daily_session[instrument.instrument_key] = local_date
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Global-context market polling failed; core quotes continue"
            )
