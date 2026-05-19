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
)
from shared.schemas import RawPost
from shared.symbols import tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("news-producer")

POLL_INTERVAL = 60  # 1 minute

async def fetch_symbol_news(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, seen_links: set):
    try:
        # Add a small random jitter to avoid hitting Yahoo all at once
        await asyncio.sleep(random.uniform(0.5, 3.0))
        
        url = f"https://finance.yahoo.com/rss/headline?s={symbol}"
        response = await client.get(url, timeout=10)
        if response.status_code != 200:
            logger.error(f"Error fetching news for {symbol}: {response.status_code}")
            return

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
            )

            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=raw_post.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=QUEUE_RAW_POSTS,
            )
            
            seen_links.add(link)
            new_count += 1

        if new_count > 0:
            logger.info(f"Symbol '{symbol}': Published {new_count} new headlines.")
            
    except Exception as e:
        logger.error(f"Error processing symbol {symbol}: {e}")

async def main():
    logger.info("Starting Yahoo Finance News Async Polling Producer...")
    
    symbols = tickers()
    logger.info(f"Tracking symbols: {symbols}")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    seen_links = set()

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient() as client:
                    while True:
                        tasks = [fetch_symbol_news(symbol, client, channel, seen_links) for symbol in symbols]
                        await asyncio.gather(*tasks)

                        # Keep seen_links from growing infinitely (keep last 500)
                        if len(seen_links) > 500:
                            # Note: set is unordered, so this is a bit arbitrary, 
                            # but fine for duplicate prevention in a rolling window.
                            l = list(seen_links)
                            seen_links = set(l[-500:])

                        logger.info(f"Batch complete. Sleeping for {POLL_INTERVAL} seconds...")
                        await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped.")
