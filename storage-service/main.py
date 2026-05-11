import sys
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


def main():
    db = DB()
    print("Connected to Postgres; schema applied.")

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=INPUT_QUEUE, durable=True)

    def on_message(ch, method, properties, body):
        try:
            post = ScoredPost.model_validate_json(body)
        except Exception as e:
            print(f"Malformed message, dropping: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            inserted = db.insert_scored(post)
            action = "inserted" if inserted else "skipped (duplicate)"
            print(f"{post.id}: {action}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except psycopg.OperationalError as e:
            print(f"DB unavailable, requeuing {post.id}: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=INPUT_QUEUE, on_message_callback=on_message)

    print(f"Listening on '{INPUT_QUEUE}'. Ctrl+C to stop.")
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
        connection.close()


if __name__ == "__main__":
    main()
