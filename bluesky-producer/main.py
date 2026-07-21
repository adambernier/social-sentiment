import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
from atproto import AsyncClient
from pydantic import ValidationError

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    QUEUE_RAW_POSTS,
    get_env_int,
)
from shared.schemas import RawPost
from shared.symbols import keywords_map, match_symbol, run_with_symbol_registry
from shared.metrics import start_metrics_server, POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bluesky-producer")

POLL_INTERVAL = get_env_int("BLUESKY_POLL_INTERVAL", 900)
MAX_BACKOFF = get_env_int("BLUESKY_MAX_BACKOFF", 3600)

async def main():
    logger.info("Starting Bluesky Polling Producer...")
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
                    kw_map = keywords_map()
                    rate_limited = False

                    for symbol, terms in kw_map.items():
                        if rate_limited:
                            break
                        for term in terms:
                            if rate_limited:
                                break
                            try:
                                params = {'q': term, 'limit': 25, 'sort': 'latest'}
                                if term in last_seen:
                                    params['since'] = last_seen[term]

                                response = await client.app.bsky.feed.search_posts(params=params)
                                
                                new_posts_count = 0
                                for post in response.posts:
                                    if term in last_seen and post.record.created_at <= last_seen[term]:
                                        continue
                                    
                                    # Post-fetch precision filtering for ambiguous tickers (SMH, MU)
                                    if not match_symbol(post.record.text, symbol):
                                        continue

                                    try:
                                        ts_str = post.record.created_at.replace("Z", "+00:00")
                                        timestamp = datetime.fromisoformat(ts_str)
                                    except Exception:
                                        timestamp = datetime.now(timezone.utc)

                                    engagement = max(1, int((post.like_count or 0) + 2 * (post.repost_count or 0) + 3 * (post.reply_count or 0)))

                                    raw_post = RawPost(
                                        id=post.cid,
                                        symbol=symbol,  # Tag with the target symbol
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

                                if new_posts_count > 0:
                                    logger.info(f"Term '{term}' ({symbol}): Published {new_posts_count} new posts.")

                                # Rate limiting safety: Sleep between queries to the public API
                                await asyncio.sleep(2.0)
                            except Exception as e:
                                logger.error(f"Error searching for term '{term}': {e}")
                                rate_limited = True

                    if rate_limited:
                        RATE_LIMITS_HIT_TOTAL.labels(platform="bluesky").inc()
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        logger.warning(f"Error/Rate limit hit. Backing off for {backoff}s.")
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
