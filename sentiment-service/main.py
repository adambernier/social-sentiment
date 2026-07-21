import asyncio
import logging
import sys
import time
from pathlib import Path

import aio_pika
from pydantic import ValidationError

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import CleanPost, ScoredPost
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_CLEAN_POSTS as INPUT_QUEUE,
    QUEUE_SCORED_POSTS as OUTPUT_QUEUE,
)
from model import SentimentModel
from shared.metrics import start_metrics_server, MESSAGES_PROCESSED_TOTAL
from shared.messaging import (
    dead_letter_message,
    declare_dead_letter_queue,
    requeue_unprocessed,
    retry_or_dead_letter,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("sentiment-service")

BATCH_SIZE = 32
BATCH_TIMEOUT = 1.0  # seconds


async def process_batch(
    batch_posts: list[CleanPost],
    batch_messages: list[aio_pika.IncomingMessage],
    model: SentimentModel,
    channel: aio_pika.Channel,
) -> None:
    texts = [post.text for post in batch_posts]
    try:
        results = await asyncio.to_thread(model.predict_batch, texts)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Sentiment inference failed for batch: {e}", exc_info=True)
        for message in batch_messages:
            await retry_or_dead_letter(message, channel, INPUT_QUEUE, e)
        return

    if len(results) != len(batch_posts):
        error = RuntimeError(
            "sentiment model returned "
            f"{len(results)} results for {len(batch_posts)} posts"
        )
        logger.error(str(error))
        for message in batch_messages:
            await retry_or_dead_letter(message, channel, INPUT_QUEUE, error)
        return

    processed_count = 0
    for post, (label, scores), message in zip(
        batch_posts,
        results,
        batch_messages,
    ):
        try:
            scored = ScoredPost(
                **post.model_dump(),
                sentiment=label,
                scores=scores,
            )
        except ValidationError as e:
            logger.error(
                f"Invalid sentiment result for {post.id}; "
                f"moving input to dead-letter queue: {e}"
            )
            await dead_letter_message(message, channel, INPUT_QUEUE, e)
            continue

        try:
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=scored.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=OUTPUT_QUEUE,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            disposition = await retry_or_dead_letter(
                message,
                channel,
                INPUT_QUEUE,
                e,
            )
            logger.error(
                f"Error publishing scored post {post.id}; {disposition}: {e}"
            )
            continue

        # If acknowledgement fails, let the worker's outer error handler return
        # any still-unprocessed inputs. The published duplicate is safe because
        # storage ingestion is idempotent within (platform, id, symbol).
        await message.ack()
        processed_count += 1

    if processed_count:
        MESSAGES_PROCESSED_TOTAL.labels(service="sentiment").inc(
            processed_count
        )


async def batch_worker(queue: asyncio.Queue, model: SentimentModel, channel: aio_pika.Channel):
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
                logger.info(f"Processing batch of {len(batch_posts)} posts...")
                await process_batch(
                    batch_posts,
                    batch_messages,
                    model,
                    channel,
                )

                batch_posts = []
                batch_messages = []
                last_flush = time.time()

        except asyncio.CancelledError:
            # Requeue any messages that are in the current batch if cancelled
            for message in batch_messages:
                try:
                    await requeue_unprocessed(message)
                except Exception:
                    pass
            break
        except Exception as e:
            logger.error(f"Error in batch worker loop: {e}", exc_info=True)
            for message in batch_messages:
                try:
                    await requeue_unprocessed(message)
                except Exception:
                    pass
            batch_posts = []
            batch_messages = []
            await asyncio.sleep(1)


async def main():
    logger.info("Loading sentiment model...")
    start_metrics_server(8009)
    model = SentimentModel()
    logger.info("Sentiment model loaded successfully.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    queue = asyncio.Queue()

    while True:
        try:
            logger.info("Connecting to RabbitMQ...")
            connection = await aio_pika.connect_robust(rabbit_url)

            async with connection:
                channel = await connection.channel()
                # Prefetch a decent amount to allow local queue buffering
                await channel.set_qos(prefetch_count=BATCH_SIZE * 2)

                await channel.declare_queue(INPUT_QUEUE, durable=True)
                await channel.declare_queue(OUTPUT_QUEUE, durable=True)
                await declare_dead_letter_queue(channel, INPUT_QUEUE)

                # Start background batch worker
                worker_task = asyncio.create_task(batch_worker(queue, model, channel))

                logger.info(f"Listening on '{INPUT_QUEUE}' (Batch size: {BATCH_SIZE}, Timeout: {BATCH_TIMEOUT}s)...")

                try:
                    # Fetch messages and push to local queue
                    input_queue = await channel.declare_queue(INPUT_QUEUE, durable=True)
                    async with input_queue.iterator() as queue_iter:
                        async for message in queue_iter:
                            try:
                                post = CleanPost.model_validate_json(message.body)
                                await queue.put((message, post))
                            except ValidationError as e:
                                logger.error(
                                    "Malformed message, moving to dead-letter "
                                    f"queue: {e}"
                                )
                                await dead_letter_message(
                                    message,
                                    channel,
                                    INPUT_QUEUE,
                                    e,
                                )
                finally:
                    worker_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass
                    # Drain and nack queue
                    logger.info(f"Shutting down: nacking {queue.qsize()} buffered messages in local queue...")
                    while not queue.empty():
                        try:
                            message, _ = queue.get_nowait()
                            await requeue_unprocessed(message)
                        except Exception as e:
                            logger.error(f"Error nacking message during shutdown: {e}")

        except Exception as e:
            logger.error(f"Error in sentiment consumer connection: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="sentiment-service")
