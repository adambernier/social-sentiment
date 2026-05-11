import json
import sys
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

    def on_message(ch, method, properties, body):
        try:
            post = CleanPost.model_validate_json(body)
            label, scores = model.predict(post.text)
            scored = ScoredPost(**post.model_dump(), sentiment=label, scores=scores)
            ch.basic_publish(
                exchange="",
                routing_key=OUTPUT_QUEUE,
                body=scored.model_dump_json().encode(),
                properties=pika.BasicProperties(delivery_mode=2),
            )
            print(f"{post.id}: {label} ({scores[label]:.2f})")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            print(f"Error processing message: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

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
