import asyncio
import logging
import sys
import hashlib
import random
from datetime import datetime, timezone
from pathlib import Path

import feedparser
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
    get_env,
    get_env_int,
)
from shared.schemas import RawPost
from shared.symbols import tickers
from shared.metrics import start_metrics_server, POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("news-producer")

# News changes slowly, so poll gently. The previous 60s cadence across 10 symbols
# (~600 req/hr) with a default user-agent got the IP rate-limited (HTTP 429) by
# Yahoo, which halted ingestion entirely. Default 15 min; tune via env.
POLL_INTERVAL = get_env_int("NEWS_POLL_INTERVAL", 900)
# When throttled, exponentially back off (up to this cap) so we stop hammering and
# let Yahoo's rate limit reset instead of staying blocked.
MAX_BACKOFF = get_env_int("NEWS_MAX_BACKOFF", 3600)
# Yahoo 429s the default httpx user-agent outright; a browser-like UA is required.
USER_AGENT = get_env(
    "NEWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

async def fetch_symbol_news(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, seen_links: set) -> bool:
    """Fetch and publish new headlines for one symbol.

    Returns True if the request was rate-limited (HTTP 429), so the caller can
    back off; False otherwise.
    """
    try:
        # Spread requests over several seconds so the batch isn't a burst.
        await asyncio.sleep(random.uniform(0.5, 8.0))

        # Hit the canonical feed host directly. finance.yahoo.com/rss/headline
        # 301-redirects here; going direct avoids the extra round-trip (the client
        # also follows redirects as a safety net).
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        response = await client.get(url)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) fetching news for {symbol}")
            return True
        if response.status_code != 200:
            logger.error(f"Error fetching news for {symbol}: {response.status_code}")
            return False

        # Use feedparser on the response content
        feed = feedparser.parse(response.text)
        
        new_count = 0
        for entry in feed.entries:
            link = entry.link
            if link in seen_links:
                continue
            
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title}. {summary}" if summary else title
            
            post_id = f"news_{hashlib.md5(link.encode()).hexdigest()}"
            
            try:
                dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                dt = datetime.now(timezone.utc)

            raw_post = RawPost(
                id=post_id,
                symbol=symbol,
                platform="yahoo",
                text=full_text,
                timestamp=dt,
                engagement=15,
            )

            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=raw_post.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=QUEUE_RAW_POSTS,
            )
            
            POSTS_INGESTED_TOTAL.labels(platform="yahoo", symbol=symbol).inc()
            seen_links.add(link)
            new_count += 1

        if new_count > 0:
            logger.info(f"Symbol '{symbol}': Published {new_count} new headlines.")
        return False

    except Exception as e:
        logger.error(f"Error processing symbol {symbol}: {e}")
        return False

async def main():
    logger.info("Starting Yahoo Finance News Async Polling Producer...")
    start_metrics_server(8004)
    
    logger.info(f"Tracking symbols: {tickers()}")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    seen_links = set()

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient(
                    headers=REQUEST_HEADERS, follow_redirects=True, timeout=10
                ) as client:
                    backoff = POLL_INTERVAL
                    while True:
                        # Re-read each poll so runtime symbol additions are honored
                        # without a restart (shared.symbols refreshes from the DB).
                        symbols = tickers()
                        tasks = [fetch_symbol_news(symbol, client, channel, seen_links) for symbol in symbols]
                        results = await asyncio.gather(*tasks)
                        rate_limited = any(results)

                        # Keep seen_links from growing infinitely (keep last 500)
                        if len(seen_links) > 500:
                            # Note: set is unordered, so this is a bit arbitrary,
                            # but fine for duplicate prevention in a rolling window.
                            l = list(seen_links)
                            seen_links = set(l[-500:])

                        # Exponential backoff while throttled; reset once requests
                        # go through, so a transient limit doesn't slow us forever.
                        if rate_limited:
                            RATE_LIMITS_HIT_TOTAL.labels(platform="yahoo").inc()
                            backoff = min(backoff * 2, MAX_BACKOFF)
                            logger.warning(
                                f"Throttled by Yahoo (429). Backing off for {backoff}s "
                                "to let the rate limit reset."
                            )
                        else:
                            if backoff != POLL_INTERVAL:
                                logger.info("Requests succeeding again; resetting poll interval.")
                            backoff = POLL_INTERVAL

                        logger.info(f"Batch complete. Sleeping for {backoff} seconds...")
                        await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="news-producer")
