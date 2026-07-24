"""Paced two-year daily backfill for factors and active U.S. symbols."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
sys.path.append(str(ROOT / "storage-service"))

from global_market import YahooMarketDataAdapter, instrument_from_row

try:
    from storage_service.db import DB
except ModuleNotFoundError:  # Repository layout uses ``storage-service``.
    from db import DB

from shared.config import GLOBAL_CONTEXT_ENABLED
from shared.global_context import InstrumentMetadata
from shared.metrics import (
    GLOBAL_BACKFILL_PROGRESS,
    GLOBAL_PROVIDER_REQUESTS_TOTAL,
)
from shared.pacing import AsyncRateLimiter

logger = logging.getLogger("global-backfill")


async def backfill_instrument(
    db: DB,
    adapter: YahooMarketDataAdapter,
    limiter: AsyncRateLimiter,
    instrument: InstrumentMetadata,
    *,
    start: datetime,
    end: datetime,
) -> int:
    await limiter.acquire()
    try:
        bars = await asyncio.to_thread(
            adapter.fetch_bars,
            instrument,
            "1d",
            start,
            end,
        )
        stored = await asyncio.to_thread(db.upsert_global_bars, bars)
        GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
            provider=adapter.provider_name,
            data_type="backfill_1d",
            status="success" if bars else "no_data",
        ).inc()
        GLOBAL_BACKFILL_PROGRESS.labels(
            provider=adapter.provider_name,
            instrument_key=instrument.instrument_key,
        ).set(1.0)
        return stored
    except Exception:
        GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
            provider=adapter.provider_name,
            data_type="backfill_1d",
            status="error",
        ).inc()
        raise


async def run_backfill(instrument_key: str | None = None) -> None:
    if not GLOBAL_CONTEXT_ENABLED:
        raise RuntimeError(
            "GLOBAL_CONTEXT_ENABLED must be true to run the global backfill"
        )
    db = DB()
    db.ensure_us_equity_instruments(db.list_active_symbols())
    instruments = [
        instrument
        for instrument in (
            instrument_from_row(row) for row in db.list_global_instruments()
        )
        if instrument is not None
        and (instrument_key is None or instrument.instrument_key == instrument_key)
    ]
    if instrument_key and not instruments:
        raise ValueError(f"Unknown or inactive instrument: {instrument_key}")

    adapter = YahooMarketDataAdapter()
    limiter = AsyncRateLimiter(max_rate=10, period=60.0)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=365 * 2 + 10)
    total = 0
    for index, instrument in enumerate(instruments, start=1):
        logger.info(
            "Backfilling %s (%s/%s)",
            instrument.instrument_key,
            index,
            len(instruments),
        )
        try:
            total += await backfill_instrument(
                db,
                adapter,
                limiter,
                instrument,
                start=start,
                end=end,
            )
        except Exception:
            logger.exception("Backfill failed for %s", instrument.instrument_key)
    logger.info("Backfill complete; upserted %s daily bars", total)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill two years of normalized daily global context",
    )
    parser.add_argument(
        "--instrument-key",
        help="Backfill one stable instrument key instead of the full universe",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(args.instrument_key))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
