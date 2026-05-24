import asyncio
import sys
import logging
from pathlib import Path

import aio_pika

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import RawPost, CleanPost
from shared.topics import TopicModel
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_RAW_POSTS as INPUT_QUEUE,
    QUEUE_CLEAN_POSTS as OUTPUT_QUEUE,
)
from preprocess import clean_text, is_valid

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("preprocessing-service")


async def process_message(message: aio_pika.IncomingMessage, topic_model: TopicModel, channel: aio_pika.Channel):
    async with message.process():
        try:
            raw = RawPost.model_validate_json(message.body)
        except Exception as e:
            logger.error(f"Malformed message, dropping: {e}")
            return

        cleaned = clean_text(raw.text)
        if not is_valid(cleaned):
            logger.info(f"{raw.id}: dropped (too short after cleaning)")
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
        )

        # Publish asynchronously
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=clean.model_dump_json().encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=OUTPUT_QUEUE,
        )
        logger.info(f"{raw.id} ({raw.symbol}): topic='{topic_label}', text='{cleaned[:70]}'")


async def main():
    logger.info("Initializing Topic Model...")
    topic_model = TopicModel()
    logger.info("Topic Model loaded successfully.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"

    while True:
        try:
            logger.info(f"Connecting to RabbitMQ at {RABBIT_HOST}:{RABBIT_PORT}...")
            connection = await aio_pika.connect_robust(rabbit_url)
            
            async with connection:
                channel = await connection.channel()
                # Prefetch count of 10 to balance backpressure
                await channel.set_qos(prefetch_count=10)

                input_queue = await channel.declare_queue(INPUT_QUEUE, durable=True)
                await channel.declare_queue(OUTPUT_QUEUE, durable=True)

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
