import sys
import time
from pathlib import Path

import pika
import psycopg

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

BATCH_SIZE = 100
BATCH_TIMEOUT = 1.0  # seconds

def main():
    db = DB()
    print("Connected to Postgres; schema applied.")

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=INPUT_QUEUE, durable=True)

    # Prefetch a full batch
    channel.basic_qos(prefetch_count=BATCH_SIZE)

    batch_posts = []
    batch_methods = []
    last_flush = time.time()

    print(f"Listening on '{INPUT_QUEUE}' (Batch size: {BATCH_SIZE}, Timeout: {BATCH_TIMEOUT}s). Ctrl+C to stop.")

    try:
        # Use consume() generator for manual batching
        for method, properties, body in channel.consume(INPUT_QUEUE, inactivity_timeout=0.1):
            if method:
                try:
                    post = ScoredPost.model_validate_json(body)
                    batch_posts.append(post)
                    batch_methods.append(method)
                except Exception as e:
                    print(f"Malformed message, dropping: {e}")
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

            # Flush batch
            now = time.time()
            if (len(batch_posts) >= BATCH_SIZE) or (batch_posts and now - last_flush > BATCH_TIMEOUT):
                print(f"Inserting batch of {len(batch_posts)} posts...")
                try:
                    count = db.insert_scored_batch(batch_posts)
                    print(f"Batch inserted: {count} rows affected.")
                    
                    # Acknowledge all in batch
                    for m in batch_methods:
                        channel.basic_ack(delivery_tag=m.delivery_tag)
                except Exception as e:
                    print(f"Database error during batch insert: {e}")
                    # In case of error, requeue individually or handle as needed
                    # For simplicity here, we requeue all
                    for m in batch_methods:
                        channel.basic_nack(delivery_tag=m.delivery_tag, requeue=True)

                batch_posts = []
                batch_methods = []
                last_flush = now

    except KeyboardInterrupt:
        print("\nStopping...")
        channel.cancel()
        connection.close()


if __name__ == "__main__":
    main()
