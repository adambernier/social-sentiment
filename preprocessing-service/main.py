import sys
from pathlib import Path

import pika

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import RawPost, CleanPost
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_RAW_POSTS as INPUT_QUEUE,
    QUEUE_CLEAN_POSTS as OUTPUT_QUEUE,
)
from preprocess import clean_text, is_valid


def main():
    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=INPUT_QUEUE, durable=True)
    channel.queue_declare(queue=OUTPUT_QUEUE, durable=True)

    def on_message(ch, method, properties, body):
        try:
            raw = RawPost.model_validate_json(body)
        except Exception as e:
            print(f"Malformed message, dropping: {e}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        cleaned = clean_text(raw.text)
        if not is_valid(cleaned):
            print(f"{raw.id}: dropped (too short after cleaning)")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        # Explicitly pass all fields including the new 'symbol' field
        clean = CleanPost(
            id=raw.id,
            symbol=raw.symbol,
            platform=raw.platform,
            text=cleaned,
            timestamp=raw.timestamp,
        )
        ch.basic_publish(
            exchange="",
            routing_key=OUTPUT_QUEUE,
            body=clean.model_dump_json().encode(),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        print(f"{raw.id} ({raw.symbol}): '{cleaned[:70]}'")
        ch.basic_ack(delivery_tag=method.delivery_tag)

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
