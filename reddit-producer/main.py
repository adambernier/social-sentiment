import asyncio
import logging
import sys
import re
import html
from datetime import datetime, timezone
from collections import deque
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
    REDDIT_USER_AGENT,
    get_env_int,
)
from shared.schemas import RawPost
from shared.symbols import keywords_map, match_symbol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("reddit-producer")

# Reddit JSON multireddit comments feed
SUBREDDITS = (
    "wallstreetbets+stocks+investing+SecurityAnalysis"
    "+options+StockMarket+semiconductors+Spacestocks"
)
FEED_URL = f"https://www.reddit.com/r/{SUBREDDITS}/comments.json?limit=100"
POLL_INTERVAL = get_env_int("REDDIT_POLL_INTERVAL", 300)
MAX_BACKOFF = get_env_int("REDDIT_MAX_BACKOFF", 3600)

# Global deque for ID-based deduplication
seen_ids = deque(maxlen=2000)

async def fetch_and_process(client: httpx.AsyncClient, channel: aio_pika.Channel) -> tuple[bool, int]:
    try:
        logger.info(f"Fetching RSS feed: {FEED_URL}")
        resp = await client.get(FEED_URL, timeout=15)
        
        if resp.status_code == 403:
            logger.error("Reddit 403 Forbidden. Check User-Agent or IP block.")
            return True, 300
        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            logger.warning(f"Reddit 429 Rate Limited. Retry-After header: {retry_after}s.")
            return True, retry_after
        elif resp.status_code != 200:
            logger.error(f"Reddit JSON error: HTTP {resp.status_code}")
            return True, 0

        data = resp.json()
        children = data.get("data", {}).get("children", [])
        
        kw_map = keywords_map()
        new_posts_count = 0
        total_entries = len(children)
        unseen_count = 0
        matched_symbols = set()

        for child in children:
            comment = child.get("data", {})
            
            # 1. Extract ID and Dedup
            post_id = comment.get("id", "")
            if not post_id:
                continue
            
            if post_id in seen_ids:
                continue
            
            unseen_count += 1
            # Add to seen_ids immediately
            seen_ids.append(post_id)

            # 2. Build Searchable Text
            body = comment.get("body", "")
            if not body:
                continue
                
            haystack = body
            
            # 3. Keyword Match
            for symbol in kw_map.keys():
                if match_symbol(haystack, symbol):
                    # 4. Publish
                    created_utc = comment.get("created_utc")
                    ts = datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else datetime.now(timezone.utc)
                    
                    engagement = max(1, int(comment.get("score", 1)))
                    
                    raw_post = RawPost(
                        id=f"rd_{post_id}",
                        symbol=symbol,
                        platform="reddit",
                        text=body.strip()[:2000],
                        timestamp=ts,
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
                    matched_symbols.add(symbol)

        if new_posts_count > 0:
            logger.info(f"Cycle complete. Published {new_posts_count} new Reddit comments across {len(matched_symbols)} symbols. (Feed: {total_entries}, New: {unseen_count})")
        else:
            logger.info(f"Cycle complete. No new matches. (Feed: {total_entries}, New: {unseen_count})")
            
        return False, 0

    except Exception as e:
        logger.error(f"Error in fetch_and_process: {e}", exc_info=True)
        return True, 0

async def main():
    logger.info("Starting Reddit RSS Producer...")
    
    # Initialize seen_ids with a "drain" fetch to avoid re-publishing old posts on startup
    # We do one quick fetch to populate the deque.
    async with httpx.AsyncClient(headers={"User-Agent": REDDIT_USER_AGENT}) as client:
        try:
            logger.info("Performing startup drain fetch to populate seen_ids...")
            resp = await client.get(FEED_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                for child in children:
                    pid = child.get("data", {}).get("id", "")
                    if pid:
                        seen_ids.append(pid)
            logger.info(f"Startup drain complete. Seeded {len(seen_ids)} IDs.")
        except Exception as e:
            logger.warning(f"Startup drain failed: {e}. Will proceed anyway.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient(headers={"User-Agent": REDDIT_USER_AGENT}) as client:
                    backoff = POLL_INTERVAL
                    while True:
                        rate_limited, requested_backoff = await fetch_and_process(client, channel)
                        
                        if rate_limited:
                            if requested_backoff > 0:
                                backoff = min(requested_backoff, MAX_BACKOFF)
                            else:
                                backoff = min(backoff * 2, MAX_BACKOFF)
                            logger.warning(f"Rate limited by Reddit. Backing off for {backoff}s.")
                        else:
                            if backoff != POLL_INTERVAL:
                                logger.info("Requests succeeding again; resetting poll interval.")
                            backoff = POLL_INTERVAL

                        logger.info(f"Batch complete. Sleeping for {backoff} seconds...")
                        await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="reddit-producer")
