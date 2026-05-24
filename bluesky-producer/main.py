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
)
from shared.schemas import RawPost
from shared.symbols import keywords_map, match_symbol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bluesky-producer")

POLL_INTERVAL = 60  # 1 minute

async def main():
    logger.info("Starting Bluesky Polling Producer...")
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

                while True:
                    kw_map = keywords_map()

                    for symbol, terms in kw_map.items():
                        for term in terms:
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
                                    new_posts_count += 1
                                    
                                    if term not in last_seen or post.record.created_at > last_seen[term]:
                                        last_seen[term] = post.record.created_at

                                if new_posts_count > 0:
                                    logger.info(f"Term '{term}' ({symbol}): Published {new_posts_count} new posts.")
                            except Exception as e:
                                logger.error(f"Error searching for term '{term}': {e}")

                    logger.info(f"Batch complete. Sleeping for {POLL_INTERVAL} seconds...")
                    await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 5s...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="bluesky-producer")
