import asyncio
import logging
import sys
from datetime import datetime, timezone
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
)
from shared.schemas import RawPost
from shared.symbols import tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stocktwits-producer")

POLL_INTERVAL = 60  # 1 minute

async def fetch_symbol(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, last_seen_ids: dict):
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        params_api = {}
        if last_seen_ids[symbol] > 0:
            params_api["since"] = last_seen_ids[symbol]

        response = await client.get(url, params=params_api, timeout=10)
        if response.status_code != 200:
            logger.error(f"Error fetching {symbol} from StockTwits: {response.status_code}")
            return

        data = response.json()
        messages = data.get("messages", [])
        
        new_count = 0
        for msg in reversed(messages):  # Process oldest to newest
            msg_id = str(msg["id"])
            body = msg["body"]
            created_at_str = msg["created_at"]
            
            try:
                timestamp = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            except Exception:
                timestamp = datetime.now(timezone.utc)

            likes = msg.get("likes", {}).get("total", 0) if isinstance(msg.get("likes"), dict) else 0
            engagement = max(1, int(likes))

            raw_post = RawPost(
                id=f"st_{msg_id}",
                symbol=symbol,
                platform="stocktwits",
                text=body,
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
            
            new_count += 1
            if msg["id"] > last_seen_ids[symbol]:
                last_seen_ids[symbol] = msg["id"]

        if new_count > 0:
            logger.info(f"Symbol '{symbol}': Published {new_count} new messages.")
            
    except Exception as e:
        logger.error(f"Error processing symbol {symbol}: {e}")

async def main():
    logger.info("Starting StockTwits Async Polling Producer...")
    
    symbols = tickers()
    logger.info(f"Tracking symbols: {symbols}")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    last_seen_ids = {symbol: 0 for symbol in symbols}

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient() as client:
                    while True:
                        tasks = [fetch_symbol(symbol, client, channel, last_seen_ids) for symbol in symbols]
                        await asyncio.gather(*tasks)

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
