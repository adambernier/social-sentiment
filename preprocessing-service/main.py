import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
from pydantic import ValidationError

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from preprocess import clean_text, is_valid

from shared.config import QUEUE_CLEAN_POSTS as OUTPUT_QUEUE
from shared.config import QUEUE_RAW_POSTS as INPUT_QUEUE
from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
)
from shared.messaging import (
    dead_letter_message,
    declare_dead_letter_queue,
    requeue_unprocessed,
    retry_or_dead_letter,
)
from shared.metrics import MESSAGES_PROCESSED_TOTAL, start_metrics_server
from shared.schemas import CleanPost, RawPost
from shared.topics import TopicModel

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("preprocessing-service")


async def process_message(message: aio_pika.IncomingMessage, topic_model: TopicModel, channel: aio_pika.Channel):
    try:
        raw = RawPost.model_validate_json(message.body)
    except ValidationError as e:
        logger.error(f"Malformed message, moving to dead-letter queue: {e}")
        await dead_letter_message(message, channel, INPUT_QUEUE, e)
        return

    try:
        cleaned = clean_text(raw.text)
        if not is_valid(cleaned):
            logger.info(f"{raw.id}: dropped (too short after cleaning)")
            await message.ack()
            return

        # Classify topic (offload CPU-bound inference to thread executor)
        topic_id, topic_label = await asyncio.to_thread(topic_model.predict, cleaned)

        # Create clean post model
        clean = CleanPost(
            id=raw.id,
            symbol=raw.symbol,
            platform=raw.platform,
            text=cleaned,
            timestamp=raw.timestamp,
            topic_id=topic_id,
            topic_label=topic_label,
            engagement=raw.engagement,
            ingested_at=raw.ingested_at,
            engagement_observed_at=raw.engagement_observed_at,
            source_schema_version=raw.source_schema_version,
            pipeline_git_commit=raw.pipeline_git_commit,
            cleaned_at=datetime.now(timezone.utc),
            topic_scored_at=datetime.now(timezone.utc),
            topic_model_version=topic_model.version,
            topic_model_hash=topic_model.model_hash,
        )

        # Publish asynchronously
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=clean.model_dump_json().encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=OUTPUT_QUEUE,
        )
    except asyncio.CancelledError:
        await requeue_unprocessed(message)
        raise
    except Exception as e:
        disposition = await retry_or_dead_letter(
            message,
            channel,
            INPUT_QUEUE,
            e,
        )
        logger.error(f"{raw.id}: processing failed; {disposition}: {e}")
        return

    await message.ack()
    MESSAGES_PROCESSED_TOTAL.labels(service="preprocessing").inc()
    logger.info(f"{raw.id} ({raw.symbol}): topic='{topic_label}', text='{cleaned[:70]}'")


async def main():
    logger.info("Initializing Topic Model...")
    start_metrics_server(8007)
    topic_model = TopicModel()
    logger.info("Topic Model loaded successfully.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"

    while True:
        try:
            logger.info(f"Connecting to RabbitMQ at {RABBIT_HOST}:{RABBIT_PORT}...")
            connection = await aio_pika.connect_robust(rabbit_url)
            
            async with connection:
                channel = await connection.channel(on_return_raises=True)
                # Prefetch count of 10 to balance backpressure
                await channel.set_qos(prefetch_count=10)

                input_queue = await channel.declare_queue(INPUT_QUEUE, durable=True)
                await channel.declare_queue(OUTPUT_QUEUE, durable=True)
                await declare_dead_letter_queue(channel, INPUT_QUEUE)

                logger.info(f"Listening on '{INPUT_QUEUE}'. Ready to consume...")
                
                async with input_queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        await process_message(message, topic_model, channel)

        except Exception as e:
            logger.error(f"Error in consumer loop: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="preprocessing-service")
