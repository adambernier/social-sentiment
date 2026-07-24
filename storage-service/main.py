import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aio_pika
import psycopg
from pydantic import ValidationError

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
from shared.messaging import (
    RequeueError,
    dead_letter_message,
    declare_dead_letter_queue,
    requeue_buffered_messages,
    requeue_unprocessed_messages,
)
from shared.runtime import supervise_long_running

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("storage-service")

BATCH_SIZE = 100
BATCH_TIMEOUT = 1.0  # seconds


@dataclass(frozen=True)
class BatchWriteResult:
    stored_messages: int
    affected_rows: int
    dead_lettered_messages: int


PERMANENT_POST_ERRORS = (psycopg.DataError, psycopg.IntegrityError)


async def _persist_scored_individually(
    posts: list[ScoredPost],
    messages: list[aio_pika.IncomingMessage],
    db: DB,
    channel: aio_pika.Channel,
) -> BatchWriteResult:
    """Store valid posts and dead-letter only permanently invalid posts."""
    stored_messages = 0
    affected_rows = 0
    dead_lettered_messages = 0

    for post, message in zip(posts, messages):
        try:
            rows = await db.insert_scored_batch_async([post])
        except PERMANENT_POST_ERRORS as error:
            logger.error(
                "Post %s caused a permanent database error; moving it to "
                "the dead-letter queue: %s",
                post.id,
                error,
                exc_info=True,
            )
            await dead_letter_message(
                message,
                channel,
                INPUT_QUEUE,
                error,
            )
            dead_lettered_messages += 1
            continue

        await message.ack()
        stored_messages += 1
        affected_rows += rows

    return BatchWriteResult(
        stored_messages=stored_messages,
        affected_rows=affected_rows,
        dead_lettered_messages=dead_lettered_messages,
    )


async def persist_scored_batch(
    posts: list[ScoredPost],
    messages: list[aio_pika.IncomingMessage],
    db: DB,
    channel: aio_pika.Channel,
) -> BatchWriteResult:
    """Persist one batch without letting a poison post retry valid neighbors.

    Data and integrity failures can be caused by one payload, so those batches
    are retried one post at a time. Connection and unexpected failures
    propagate so the worker returns every unprocessed message to RabbitMQ.
    """
    try:
        affected_rows = await db.insert_scored_batch_async(posts)
    except PERMANENT_POST_ERRORS as error:
        logger.warning(
            "Batch insert hit a permanent data error; isolating %s posts: %s",
            len(posts),
            error,
        )
        return await _persist_scored_individually(
            posts,
            messages,
            db,
            channel,
        )

    for message in messages:
        await message.ack()

    return BatchWriteResult(
        stored_messages=len(posts),
        affected_rows=affected_rows,
        dead_lettered_messages=0,
    )


async def db_writer_worker(
    queue: asyncio.Queue,
    db: DB,
    channel: aio_pika.Channel,
):
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
                    result = await persist_scored_batch(
                        batch_posts,
                        batch_messages,
                        db,
                        channel,
                    )
                    logger.info(
                        "Batch handled: %s posts stored "
                        "(affected rows: %s), %s dead-lettered.",
                        result.stored_messages,
                        result.affected_rows,
                        result.dead_lettered_messages,
                    )

                    if result.stored_messages:
                        MESSAGES_PROCESSED_TOTAL.labels(
                            service="storage"
                        ).inc(result.stored_messages)

                except Exception as e:
                    logger.error(
                        "Storage batch failed before all messages were "
                        "handled: %s",
                        e,
                        exc_info=True,
                    )
                    await requeue_unprocessed_messages(batch_messages)

                batch_posts = []
                batch_messages = []
                last_flush = time.time()

        except asyncio.CancelledError:
            # Requeue any messages that are in the current batch if cancelled
            try:
                await requeue_unprocessed_messages(batch_messages)
            except RequeueError:
                logger.error(
                    "Could not requeue every in-flight storage message during "
                    "shutdown; closing the channel will return unacked messages",
                    exc_info=True,
                )
            raise
        except RequeueError:
            raise
        except Exception as e:
            logger.error(f"Error in database writer worker: {e}", exc_info=True)
            await requeue_unprocessed_messages(batch_messages)
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
            # Align to the hour so the retention boundary cannot split one
            # sentiment bucket between the aggregate and raw-post tiers.
            post_cutoff = (now - timedelta(days=retention_days)).replace(
                minute=0, second=0, microsecond=0
            )
            quote_cutoff = now - timedelta(days=quote_retention_days)
            
            # Move expired posts into the cold tier atomically, then prune quotes.
            rolled_up, pruned_posts = await asyncio.to_thread(
                db.rollup_and_prune_posts,
                post_cutoff,
            )
            pruned_quotes = await asyncio.to_thread(db.prune_old_quotes, quote_cutoff)
            context_counts = await asyncio.to_thread(
                db.prune_global_context,
                hourly_cutoff=now - timedelta(days=180),
                daily_cutoff=now - timedelta(days=365 * 5),
                event_cutoff=now - timedelta(days=365),
            )
            
            logger.info(
                f"Scheduled database maintenance completed: "
                f"rolled up {rolled_up:,} aggregation rows, "
                f"pruned {pruned_posts:,} posts, "
                f"pruned {pruned_quotes:,} stock quotes, "
                f"{context_counts[0]:,} hourly context bars, "
                f"{context_counts[1]:,} daily context bars, and "
                f"{context_counts[2]:,} event signals."
            )
        except Exception as e:
            logger.error(f"Error in scheduled database maintenance loop: {e}", exc_info=True)
            
        await asyncio.sleep(interval_seconds)


async def consume_input_messages(
    input_queue,
    queue: asyncio.Queue,
    channel: aio_pika.Channel,
) -> None:
    async with input_queue.iterator() as queue_iter:
        async for message in queue_iter:
            try:
                post = ScoredPost.model_validate_json(message.body)
                await queue.put((message, post))
            except ValidationError as e:
                logger.error(
                    "Malformed message, moving to dead-letter queue: %s",
                    e,
                )
                await dead_letter_message(
                    message,
                    channel,
                    INPUT_QUEUE,
                    e,
                )


async def run_consumer_session(
    input_queue,
    queue: asyncio.Queue,
    db: DB,
    channel: aio_pika.Channel,
) -> None:
    try:
        await supervise_long_running(
            (
                "storage batch worker",
                lambda: db_writer_worker(queue, db, channel),
            ),
            (
                "storage queue consumer",
                lambda: consume_input_messages(input_queue, queue, channel),
            ),
            (
                "storage rollup scheduler",
                lambda: rollup_scheduler(db),
            ),
        )
    except asyncio.CancelledError:
        logger.info(
            "Shutting down: nacking %s buffered storage messages...",
            queue.qsize(),
        )
        try:
            await requeue_buffered_messages(queue)
        except RequeueError:
            logger.error(
                "Could not requeue every buffered storage message during "
                "shutdown; closing the channel will return unacked messages",
                exc_info=True,
            )
        raise
    except Exception as session_error:
        logger.info(
            "Consumer session failed: nacking %s buffered storage messages...",
            queue.qsize(),
        )
        try:
            await requeue_buffered_messages(queue)
        except RequeueError as requeue_error:
            raise requeue_error from session_error
        raise


async def main():
    logger.info("Connecting to database...")
    start_metrics_server(8008)
    db = DB()
    logger.info("Database connection established.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    while True:
        try:
            logger.info("Connecting to RabbitMQ...")
            connection = await aio_pika.connect_robust(rabbit_url)

            async with connection:
                channel = await connection.channel()
                # Prefetch count of BATCH_SIZE * 2 to keep local queue buffered
                await channel.set_qos(prefetch_count=BATCH_SIZE * 2)

                input_queue = await channel.declare_queue(
                    INPUT_QUEUE,
                    durable=True,
                )
                await declare_dead_letter_queue(channel, INPUT_QUEUE)

                queue = asyncio.Queue(maxsize=BATCH_SIZE * 2)
                logger.info(
                    "Listening on '%s' (Batch size: %s, Timeout: %ss)...",
                    INPUT_QUEUE,
                    BATCH_SIZE,
                    BATCH_TIMEOUT,
                )
                await run_consumer_session(
                    input_queue,
                    queue,
                    db,
                    channel,
                )

        except Exception as e:
            logger.error(
                "Error in storage consumer connection: %s. Retrying in 10s...",
                e,
                exc_info=True,
            )
            await asyncio.sleep(10)


if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="storage-service")
