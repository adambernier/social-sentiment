import asyncio
import logging
import sys
import re
import html
from datetime import datetime, timezone
from collections import deque
from pathlib import Path

import httpx
import feedparser
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
)
from shared.schemas import RawPost
from shared.symbols import keywords_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("reddit-producer")

# Reddit RSS multireddit feed
SUBREDDITS = (
    "wallstreetbets+stocks+investing+SecurityAnalysis"
    "+options+StockMarket+semiconductors+Spacestocks"
)
FEED_URL = f"https://www.reddit.com/r/{SUBREDDITS}/new/.rss?limit=100"
POLL_INTERVAL = 120  # 2 minutes

# Regex for extracting the base36 ID from the permalink
# Example: https://www.reddit.com/r/wallstreetbets/comments/1crk7k4/is_this_the_bottom/
ID_RE = re.compile(r"/comments/([a-z0-9]+)/")
# Regex for stripping HTML tags
TAG_RE = re.compile(r"<[^>]+>")

# Global deque for ID-based deduplication
seen_ids = deque(maxlen=2000)

async def fetch_and_process(client: httpx.AsyncClient, channel: aio_pika.Channel):
    try:
        logger.info(f"Fetching RSS feed: {FEED_URL}")
        resp = await client.get(FEED_URL, timeout=15)
        
        if resp.status_code == 403:
            logger.error("Reddit 403 Forbidden. Check User-Agent or IP block.")
            await asyncio.sleep(300)
            return
        elif resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            logger.warning(f"Reddit 429 Rate Limited. Retrying after {retry_after}s.")
            await asyncio.sleep(retry_after)
            return
        elif resp.status_code != 200:
            logger.error(f"Reddit RSS error: HTTP {resp.status_code}")
            return

        # Parse feed in a thread to avoid blocking the event loop
        feed = await asyncio.to_thread(feedparser.parse, resp.text)
        
        if feed.bozo and not feed.entries:
            logger.warning("Feedparser reported bozo error with no entries.")
            return

        kw_map = keywords_map()
        new_posts_count = 0
        matched_symbols = set()

        for entry in feed.entries:
            # 1. Extract ID and Dedup
            # Reddit RSS IDs are typically 't3_xxxxxx'
            post_id = getattr(entry, "id", "")
            if not post_id:
                continue
            
            if post_id in seen_ids:
                continue
            
            # Add to seen_ids immediately
            seen_ids.append(post_id)

            # 2. NSFW Filter
            is_nsfw = any(tag.get("term") == "nsfw" for tag in getattr(entry, "tags", []))
            if is_nsfw:
                continue

            # 3. Build Searchable Text
            title = entry.title
            content_html = entry.content[0].value if hasattr(entry, "content") and entry.content else ""
            selftext = html.unescape(TAG_RE.sub(" ", content_html)).strip()[:2000]
            haystack = f"{title}\n{selftext}"
            
            # 4. Keyword Match
            # We match terms one by one to tag the post with the correct symbol
            for symbol, terms in kw_map.items():
                match_found = False
                for term in terms:
                    # Case-insensitive boundary search
                    # We use lookaround instead of \b to correctly support cashtags like $NVDA
                    # since \b doesn't match before a '$' (non-word char)
                    pattern = rf"(?<![a-zA-Z0-9]){re.escape(term)}(?![a-zA-Z0-9])"
                    if re.search(pattern, haystack, re.I):
                        match_found = True
                        break
                
                if match_found:
                    # 5. Publish
                    # Reddit timestamps in RSS are struct_time in UTC
                    ts = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                    
                    raw_post = RawPost(
                        id=f"rd_{post_id}",
                        symbol=symbol,
                        platform="reddit",
                        text=f"{title}\n{selftext}".strip(),
                        timestamp=ts,
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
            logger.info(f"Cycle complete. Published {new_posts_count} new Reddit posts across {len(matched_symbols)} symbols.")
        else:
            logger.info("Cycle complete. No new matches.")

    except Exception as e:
        logger.error(f"Error in fetch_and_process: {e}", exc_info=True)

async def main():
    logger.info("Starting Reddit RSS Producer...")
    
    # Initialize seen_ids with a "drain" fetch to avoid re-publishing old posts on startup
    # We do one quick fetch to populate the deque.
    async with httpx.AsyncClient(headers={"User-Agent": REDDIT_USER_AGENT}) as client:
        try:
            logger.info("Performing startup drain fetch to populate seen_ids...")
            resp = await client.get(FEED_URL, timeout=10)
            if resp.status_code == 200:
                feed = await asyncio.to_thread(feedparser.parse, resp.text)
                for entry in feed.entries:
                    pid = getattr(entry, "id", "")
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
                    while True:
                        await fetch_and_process(client, channel)
                        await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"RabbitMQ connection error: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped.")
