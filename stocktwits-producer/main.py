import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
import httpx

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
from shared.pacing import AsyncRateLimiter, PerSymbolBackoff, paced_gather
from shared.schemas import RawPost
from shared.symbols import run_with_symbol_registry, tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stocktwits-producer")

POLL_INTERVAL = get_env_int("STOCKTWITS_POLL_INTERVAL", 900)
MAX_BACKOFF = get_env_int("STOCKTWITS_MAX_BACKOFF", 3600)
# Request shaping: cap in-flight requests and pace how fast new ones start so a
# large symbol list doesn't fire one big burst per cycle. Defaults preserve
# current behavior at small N (60/min easily covers ~dozens of symbols per 60s
# cycle) while smoothing the burst; tighten RATE_PER_MIN if StockTwits 429s.
MAX_CONCURRENCY = get_env_int("STOCKTWITS_MAX_CONCURRENCY", 4)
RATE_PER_MIN = get_env_int("STOCKTWITS_RATE_PER_MIN", 60)

async def fetch_symbol(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, last_seen_ids: dict) -> bool:
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        params_api = {}
        if last_seen_ids[symbol] > 0:
            params_api["since"] = last_seen_ids[symbol]

        response = await client.get(url, params=params_api, timeout=10)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) fetching StockTwits for {symbol}")
            return True
        if response.status_code != 200:
            logger.error(f"Error fetching {symbol} from StockTwits: {response.status_code}")
            return False

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
            
            POSTS_INGESTED_TOTAL.labels(platform="stocktwits", symbol=symbol).inc()
            new_count += 1
            if msg["id"] > last_seen_ids[symbol]:
                last_seen_ids[symbol] = msg["id"]

        if new_count > 0:
            logger.info(f"Symbol '{symbol}': Published {new_count} new messages.")
        
        return False
            
    except Exception as e:
        logger.error(f"Error processing symbol {symbol}: {e}")
        return False

async def main():
    logger.info("Starting StockTwits Async Polling Producer...")
    initialize_rate_limit_metrics("stocktwits")
    start_metrics_server(8006)
    
    logger.info(f"Tracking symbols: {tickers()}")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    # Per-symbol cursor (highest message id seen), kept across polls. Symbols added
    # at runtime via the admin API get a 0 cursor the first time they're seen.
    last_seen_ids: dict[str, int] = {}

    # One limiter for the lifetime of the process (survives reconnects below).
    limiter = AsyncRateLimiter(max_rate=RATE_PER_MIN, period=60.0)
    # Independent per-symbol backoff so one throttled ticker can't stall the rest.
    backoff_tracker = PerSymbolBackoff(base_interval=POLL_INTERVAL, max_backoff=MAX_BACKOFF)

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient() as client:
                    while True:
                        # Re-read each poll so runtime symbol additions are honored
                        # without a restart (shared.symbols refreshes from the DB).
                        symbols = tickers()
                        for symbol in symbols:
                            last_seen_ids.setdefault(symbol, 0)
                        # Skip symbols still in their own rate-limit cooldown.
                        due = backoff_tracker.due(symbols)
                        results = await paced_gather(
                            due,
                            lambda sym: fetch_symbol(sym, client, channel, last_seen_ids),
                            max_concurrency=MAX_CONCURRENCY,
                            limiter=limiter,
                        )
                        for sym, was_limited in zip(due, results):
                            backoff_tracker.record(sym, was_limited)
                            if was_limited:
                                RATE_LIMITS_HIT_TOTAL.labels(platform="stocktwits").inc()

                        penalized = backoff_tracker.penalized()
                        if penalized:
                            logger.warning(
                                f"{len(penalized)} symbol(s) in rate-limit backoff, "
                                f"skipped {len(symbols) - len(due)} this cycle: {penalized}"
                            )
                        logger.info(
                            f"Batch complete ({len(due)} polled). Sleeping {POLL_INTERVAL}s..."
                        )
                        await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 10s...")
            await asyncio.sleep(10)

async def service_main():
    await run_with_symbol_registry(main)


if __name__ == "__main__":
    from shared.runtime import run
    run(service_main, name="stocktwits-producer")
