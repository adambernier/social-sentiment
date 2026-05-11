import sys
from pathlib import Path
from datetime import datetime, timezone

import pika

sys.path.append(str(Path(__file__).resolve().parent))

from shared.schemas import RawPost
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_RAW_POSTS as INPUT_QUEUE,
)

samples = [
    ("reddit", "r1", "I love this product, it's amazing!"),
    ("reddit", "r2", "Check out @anthropic at https://anthropic.com &amp; let me know!"),
    ("twitter", "t1", "The   package    arrived   today.  "),
    ("twitter", "t2", "Best concert of my life last night! @friend you missed out"),
    ("reddit", "r3", "Absolutely disgusted by the customer service."),
    ("twitter", "t3", "hi"),  # should get filtered as too short
    ("reddit", "r4", "&lt;p&gt;This had HTML entities&lt;/p&gt; so annoying"),
]


def main():
    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=INPUT_QUEUE, durable=True)

    for platform, post_id, text in samples:
        post = RawPost(
            id=post_id,
            platform=platform,
            text=text,
            timestamp=datetime.now(timezone.utc),
        )
        channel.basic_publish(
            exchange="",
            routing_key=INPUT_QUEUE,
            body=post.model_dump_json().encode(),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        print(f"Published {post.id}: {text}")

    connection.close()


if __name__ == "__main__":
    main()
