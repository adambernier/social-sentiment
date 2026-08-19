import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
from atproto import AsyncClient
from atproto_client.exceptions import RequestErrorBase
from pydantic import ValidationError

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    QUEUE_RAW_POSTS,
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    get_env_int,
)
from shared.metrics import (
    POSTS_INGESTED_TOTAL,
    RATE_LIMITS_HIT_TOTAL,
    initialize_rate_limit_metrics,
    start_metrics_server,
)
from shared.schemas import RawPost
from shared.symbols import keywords_map, match_symbol, run_with_symbol_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bluesky-producer")

POLL_INTERVAL = get_env_int("BLUESKY_POLL_INTERVAL", 900)
MAX_BACKOFF = get_env_int("BLUESKY_MAX_BACKOFF", 3600)


def is_rate_limit_error(error: Exception) -> bool:
    """Return whether an AT Protocol request failed with HTTP 429."""
    return (
        isinstance(error, RequestErrorBase)
        and error.response is not None
        and error.response.status_code == 429
    )


async def search_and_publish(
    client: AsyncClient,
    channel: aio_pika.Channel,
    symbol: str,
    term: str,
    last_seen: dict[str, str],
) -> int:
    params = {"q": term, "limit": 25, "sort": "latest"}
    if term in last_seen:
        params["since"] = last_seen[term]

    response = await client.app.bsky.feed.search_posts(params=params)
    new_posts_count = 0

    for post in response.posts:
        if term in last_seen and post.record.created_at <= last_seen[term]:
            continue

        # Post-fetch precision filtering for ambiguous tickers (SMH, MU).
        if not match_symbol(post.record.text, symbol):
            continue

        try:
            timestamp = datetime.fromisoformat(
                post.record.created_at.replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError):
            timestamp = datetime.now(timezone.utc)

        engagement = max(
            1,
            int(
                (post.like_count or 0)
                + 2 * (post.repost_count or 0)
                + 3 * (post.reply_count or 0)
            ),
        )
        raw_post = RawPost(
            id=post.cid,
            symbol=symbol,
            platform="bluesky",
            text=post.record.text,
            timestamp=timestamp,
            engagement=engagement,
        )

        await channel.default_exchange.publish(
            aio_pika.Message(
                body=raw_post.model_dump_json().encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=QUEUE_RAW_POSTS,
        )
        POSTS_INGESTED_TOTAL.labels(platform="bluesky", symbol=symbol).inc()
        new_posts_count += 1

        if term not in last_seen or post.record.created_at > last_seen[term]:
            last_seen[term] = post.record.created_at

    return new_posts_count


async def poll_search_terms(
    client: AsyncClient,
    channel: aio_pika.Channel,
    keyword_mapping: dict[str, list[str]],
    last_seen: dict[str, str],
) -> bool:
    """Poll one batch, returning True only when Bluesky responds with HTTP 429."""
    for symbol, terms in keyword_mapping.items():
        for term in terms:
            try:
                new_posts_count = await search_and_publish(
                    client,
                    channel,
                    symbol,
                    term,
                    last_seen,
                )
                if new_posts_count > 0:
                    logger.info(
                        "Term %r (%s): Published %d new posts.",
                        term,
                        symbol,
                        new_posts_count,
                    )
            except ValidationError as error:
                # A newly introduced response type should skip this term, not
                # stop every symbol or masquerade as API throttling.
                logger.error(
                    "Bluesky response validation failed for term %r; "
                    "skipping this term: %s",
                    term,
                    error,
                )
            except Exception as error:
                if is_rate_limit_error(error):
                    logger.warning("Bluesky rate limit reached while searching %r", term)
                    return True
                logger.error("Error searching for term %r: %s", term, error)

            # Pace successful and non-rate-limited failed queries alike so an
            # upstream outage or bad response cannot turn into a request burst.
            await asyncio.sleep(2.0)

    return False


async def main():
    logger.info("Starting Bluesky Polling Producer...")
    initialize_rate_limit_metrics("bluesky")
    start_metrics_server(8001)
    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    client = AsyncClient(base_url='https://api.bsky.app')
    
    # Track latest post timestamp per SEARCH TERM to avoid duplicates during polling
    last_seen = {}

    while True:
        try:
            logger.info(f"Connecting to RabbitMQ at {RABBIT_HOST}...")
            connection = await aio_pika.connect_robust(rabbit_url)

            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                backoff = POLL_INTERVAL
                while True:
                    rate_limited = await poll_search_terms(
                        client,
                        channel,
                        keywords_map(),
                        last_seen,
                    )

                    if rate_limited:
                        RATE_LIMITS_HIT_TOTAL.labels(platform="bluesky").inc()
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        logger.warning(f"Rate limit hit. Backing off for {backoff}s.")
                    else:
                        if backoff != POLL_INTERVAL:
                            logger.info("Requests succeeding again; resetting poll interval.")
                        backoff = POLL_INTERVAL

                    logger.info(f"Batch complete. Sleeping for {backoff} seconds...")
                    await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

async def service_main():
    await run_with_symbol_registry(main)


if __name__ == "__main__":
    from shared.runtime import run
    run(service_main, name="bluesky-producer")
