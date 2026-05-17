import json
import sys
import time
from pathlib import Path

import pika

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

BATCH_SIZE = 32
BATCH_TIMEOUT = 1.0  # seconds

def main():
    print("Loading sentiment model...")
    model = SentimentModel()
    print("Model ready.")

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=INPUT_QUEUE, durable=True)
    channel.queue_declare(queue=OUTPUT_QUEUE, durable=True)

    # Allow RabbitMQ to send us a full batch at once
    channel.basic_qos(prefetch_count=BATCH_SIZE)

    batch_posts = []
    batch_methods = []
    last_flush = time.time()

    print(f"Listening on '{INPUT_QUEUE}' (Batch size: {BATCH_SIZE}, Timeout: {BATCH_TIMEOUT}s). Ctrl+C to stop.")

    try:
        # Use consume() as a generator to fetch messages manually
        for method, properties, body in channel.consume(INPUT_QUEUE, inactivity_timeout=0.1):
            if method:
                try:
                    post = CleanPost.model_validate_json(body)
                    batch_posts.append(post)
                    batch_methods.append(method)
                except Exception as e:
                    print(f"Malformed message, dropping: {e}")
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

            # Check if we should flush the batch
            now = time.time()
            if (len(batch_posts) >= BATCH_SIZE) or (batch_posts and now - last_flush > BATCH_TIMEOUT):
                # Process batch
                print(f"Processing batch of {len(batch_posts)} posts...")
                texts = [post.text for post in batch_posts]
                results = model.predict_batch(texts)

                for post, (label, scores), method in zip(batch_posts, results, batch_methods):
                    try:
                        scored = ScoredPost(**post.model_dump(), sentiment=label, scores=scores)
                        channel.basic_publish(
                            exchange="",
                            routing_key=OUTPUT_QUEUE,
                            body=scored.model_dump_json().encode(),
                            properties=pika.BasicProperties(delivery_mode=2),
                        )
                        channel.basic_ack(delivery_tag=method.delivery_tag)
                    except Exception as e:
                        print(f"Error publishing/acking {post.id}: {e}")
                        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

                batch_posts = []
                batch_methods = []
                last_flush = now

    except KeyboardInterrupt:
        print("\nStopping...")
        channel.cancel()
        connection.close()

if __name__ == "__main__":
    main()
