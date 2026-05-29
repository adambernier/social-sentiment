import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import aio_pika

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    QUEUE_RAW_POSTS,
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_URL,
    get_env_int,
)
from shared.schemas import RawPost
from shared.symbols import tickers, match_symbol
from shared.pacing import AsyncRateLimiter, PerSymbolBackoff, paced_gather
from shared.metrics import start_metrics_server, POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("alpaca-producer")

# Rate limiting for Alpaca
POLL_INTERVAL = get_env_int("ALPACA_POLL_INTERVAL", 900)
MAX_BACKOFF = get_env_int("ALPACA_MAX_BACKOFF", 3600)
MAX_CONCURRENCY = get_env_int("ALPACA_MAX_CONCURRENCY", 4)
RATE_PER_MIN = get_env_int("ALPACA_RATE_PER_MIN", 150) # Alpaca allows 200/min free usually

async def fetch_symbol_news(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, seen_ids: set) -> bool:
    try:
        # Alpaca News API does not require date ranges if we just want recent news
        params = {
            "symbols": symbol,
            "limit": 50,
        }
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
        }
        response = await client.get(ALPACA_URL, params=params, headers=headers)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) fetching news for {symbol}")
            return True
        if response.status_code != 200:
            logger.error(f"Error fetching news for {symbol}: {response.status_code} - {response.text}")
            return False

        data = response.json()
        articles = data.get("news", [])
        if not isinstance(articles, list):
            logger.error(f"Unexpected response for {symbol}: {articles!r:.200}")
            return False

        new_count = 0
        skipped = 0
        for art in articles:
            art_id = art.get("id")
            if art_id is None:
                continue
            post_id = f"alpaca_{art_id}"
            if post_id in seen_ids:
                continue

            headline = (art.get("headline") or "").strip()
            summary = (art.get("summary") or "").strip()
            full_text = f"{headline}. {summary}" if summary else headline
            if not full_text.strip():
                continue

            if not match_symbol(full_text, symbol):
                seen_ids.add(post_id)
                skipped += 1
                continue

            created_at_str = art.get("created_at")
            if created_at_str:
                try:
                    dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                except ValueError:
                    dt = datetime.now(timezone.utc)
            else:
                dt = datetime.now(timezone.utc)

            post = RawPost(
                id=post_id,
                symbol=symbol,
                platform="alpaca",
                text=full_text,
                timestamp=dt,
                engagement=1,
            )

            await channel.default_exchange.publish(
                aio_pika.Message(body=post.model_dump_json().encode()),
                routing_key=QUEUE_RAW_POSTS,
            )
            seen_ids.add(post_id)
            new_count += 1
            POSTS_INGESTED_TOTAL.labels(platform="alpaca", symbol=symbol).inc()

        logger.info(f"[{symbol}] Queued {new_count} new Alpaca posts (skipped {skipped})")
        return False

    except httpx.RequestError as e:
        logger.error(f"Network error fetching Alpaca for {symbol}: {e}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error for {symbol}: {e}")
        return False

async def main():
    if not ALPACA_API_KEY or not ALPACA_API_SECRET:
        logger.error("ALPACA_API_KEY and ALPACA_API_SECRET must be set")
        sys.exit(1)

    # Start metrics
    start_metrics_server(8003)

    connection = await aio_pika.connect_robust(
        host=RABBIT_HOST,
        port=RABBIT_PORT,
        login=RABBIT_USER,
        password=RABBIT_PASS,
    )
    channel = await connection.channel()
    await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

    limiter = AsyncRateLimiter(max_rate=RATE_PER_MIN, period=60.0)
    backoff_tracker = PerSymbolBackoff(base_interval=POLL_INTERVAL, max_backoff=MAX_BACKOFF)
    seen_ids = set()

    logger.info(f"Starting Alpaca producer loop using {ALPACA_URL}")
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Re-read each poll so runtime symbol additions are honored
                # without a restart (shared.symbols refreshes from the DB).
                symbols = tickers()
                # Skip symbols still in their own rate-limit cooldown.
                due = backoff_tracker.due(symbols)
                results = await paced_gather(
                    due,
                    lambda sym: fetch_symbol_news(sym, client, channel, seen_ids),
                    max_concurrency=MAX_CONCURRENCY,
                    limiter=limiter,
                )
                for sym, was_limited in zip(due, results):
                    backoff_tracker.record(sym, was_limited)
                    if was_limited:
                        RATE_LIMITS_HIT_TOTAL.labels(platform="alpaca").inc()

                if len(seen_ids) > 2000:
                    seen_ids = set(list(seen_ids)[-2000:])

                penalized = backoff_tracker.penalized()
                if penalized:
                    logger.warning(
                        f"{len(penalized)} symbol(s) in rate-limit backoff, "
                        f"skipped {len(symbols) - len(due)} this cycle: {penalized}"
                    )
                logger.info(f"Cycle complete ({len(due)} polled). Sleeping {POLL_INTERVAL}s.")
                await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.exception(f"Error in producer loop: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting cleanly")
