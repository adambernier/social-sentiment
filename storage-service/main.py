import asyncio
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aio_pika
import psycopg

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import ScoredPost
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_SCORED_POSTS as INPUT_QUEUE,
)
from db import DB
from shared.metrics import start_metrics_server, MESSAGES_PROCESSED_TOTAL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("storage-service")

BATCH_SIZE = 100
BATCH_TIMEOUT = 1.0  # seconds


async def db_writer_worker(queue: asyncio.Queue, db: DB):
    batch_posts = []
    batch_messages = []
    last_flush = time.time()

    while True:
        try:
            if not batch_posts:
                # Wait indefinitely for the first post in the batch
                message, post = await queue.get()
                batch_posts.append(post)
                batch_messages.append(message)
                last_flush = time.time()
            else:
                # Wait for the next post, but timeout when BATCH_TIMEOUT is reached
                elapsed = time.time() - last_flush
                remaining = max(0.01, BATCH_TIMEOUT - elapsed)
                try:
                    message, post = await asyncio.wait_for(queue.get(), timeout=remaining)
                    batch_posts.append(post)
                    batch_messages.append(message)
                except asyncio.TimeoutError:
                    # Timeout reached, flush what we have
                    pass

            # Flush batch if full or timeout reached
            if len(batch_posts) >= BATCH_SIZE or (batch_posts and (time.time() - last_flush >= BATCH_TIMEOUT)):
                logger.info(f"Inserting batch of {len(batch_posts)} posts...")
                try:
                    # Run database writes asynchronously
                    rows = await db.insert_scored_batch_async(batch_posts)
                    logger.info(f"Batch inserted: {len(batch_posts)} posts (affected rows: {rows}).")
                    
                    MESSAGES_PROCESSED_TOTAL.labels(service="storage").inc(len(batch_posts))

                    # Acknowledge all messages in the batch
                    for message in batch_messages:
                        await message.ack()

                except Exception as e:
                    logger.error(f"Database error during async batch insert: {e}")
                    # Requeue all messages in the batch
                    for message in batch_messages:
                        try:
                            await message.nack(requeue=True)
                        except Exception:
                            pass

                batch_posts = []
                batch_messages = []
                last_flush = time.time()

        except asyncio.CancelledError:
            # Requeue any messages that are in the current batch if cancelled
            for message in batch_messages:
                try:
                    await message.nack(requeue=True)
                except Exception:
                    pass
            break
        except Exception as e:
            logger.error(f"Error in database writer worker: {e}", exc_info=True)
            for message in batch_messages:
                try:
                    await message.nack(requeue=True)
                except Exception:
                    pass
            batch_posts = []
            batch_messages = []
            await asyncio.sleep(1)


async def rollup_scheduler(db: DB):
    """Periodically rolls up posts into hourly_sentiment_agg and prunes old data.
    
    Runs once every 24 hours. Offloads blocking synchronous database operations 
    to a background thread to prevent halting the async event loop.
    """
    retention_days = 7
    quote_retention_days = 90
    interval_seconds = 24 * 3600  # 24 hours
    
    # Wait 60 seconds on service startup before performing the first cleanup
    await asyncio.sleep(60)
    
    while True:
        try:
            logger.info("Starting scheduled database rollup and prune...")
            now = datetime.now(timezone.utc)
            # Align to the hour so only fully-elapsed hours are rolled up and pruned.
            # An unaligned cutoff aggregates a partial boundary hour, then prunes its
            # early posts; the next run re-aggregates that bucket from only the
            # surviving (later) posts and overwrites it — permanently undercounting
            # the hour. Truncating to the hour means a bucket is only ever touched
            # once its hour is complete.
            post_cutoff = (now - timedelta(days=retention_days)).replace(
                minute=0, second=0, microsecond=0
            )
            quote_cutoff = now - timedelta(days=quote_retention_days)
            
            # Execute database actions concurrently on default thread pool executor
            rolled_up = await asyncio.to_thread(db.rollup_to_aggregates, post_cutoff)
            pruned_posts = await asyncio.to_thread(db.prune_old_posts, post_cutoff)
            pruned_quotes = await asyncio.to_thread(db.prune_old_quotes, quote_cutoff)
            
            logger.info(
                f"Scheduled database maintenance completed: "
                f"rolled up {rolled_up:,} aggregation rows, "
                f"pruned {pruned_posts:,} posts, "
                f"pruned {pruned_quotes:,} stock quotes."
            )
        except Exception as e:
            logger.error(f"Error in scheduled database maintenance loop: {e}", exc_info=True)
            
        await asyncio.sleep(interval_seconds)


async def main():
    logger.info("Initializing database and applying schema...")
    start_metrics_server(8008)
    db = DB()
    logger.info("Database schema applied.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    queue = asyncio.Queue()

    while True:
        try:
            logger.info("Connecting to RabbitMQ...")
            connection = await aio_pika.connect_robust(rabbit_url)

            async with connection:
                channel = await connection.channel()
                # Prefetch count of BATCH_SIZE * 2 to keep local queue buffered
                await channel.set_qos(prefetch_count=BATCH_SIZE * 2)

                await channel.declare_queue(INPUT_QUEUE, durable=True)

                # Start the background batch database writer worker
                worker_task = asyncio.create_task(db_writer_worker(queue, db))
                # Start the background database maintenance scheduler
                scheduler_task = asyncio.create_task(rollup_scheduler(db))

                logger.info(f"Listening on '{INPUT_QUEUE}' (Batch size: {BATCH_SIZE}, Timeout: {BATCH_TIMEOUT}s)...")

                try:
                    input_queue = await channel.declare_queue(INPUT_QUEUE, durable=True)
                    async with input_queue.iterator() as queue_iter:
                        async for message in queue_iter:
                            try:
                                post = ScoredPost.model_validate_json(message.body)
                                await queue.put((message, post))
                            except Exception as e:
                                logger.error(f"Malformed message, dropping: {e}")
                                await message.nack(requeue=False)
                finally:
                    worker_task.cancel()
                    scheduler_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                    try:
                        await scheduler_task
                    except asyncio.CancelledError:
                        pass
                    # Drain and nack queue
                    logger.info(f"Shutting down: nacking {queue.qsize()} buffered messages in local queue...")
                    while not queue.empty():
                        try:
                            message, _ = queue.get_nowait()
                            await message.nack(requeue=True)
                        except Exception as e:
                            logger.error(f"Error nacking message during shutdown: {e}")

        except Exception as e:
            logger.error(f"Error in storage consumer connection: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="storage-service")
