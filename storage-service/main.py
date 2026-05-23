import asyncio
import logging
import sys
import time
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


async def main():
    logger.info("Initializing database and applying schema...")
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

                logger.info(f"Listening on '{INPUT_QUEUE}' (Batch size: {BATCH_SIZE}, Timeout: {BATCH_TIMEOUT}s)...")

                try:
                    async with connection:
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
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                    # Drain queue
                    while not queue.empty():
                        queue.get_nowait()

        except Exception as e:
            logger.error(f"Error in storage consumer connection: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped.")
