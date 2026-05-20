import sys
import time
import re
from pathlib import Path
import pika
import json
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import ScoredPost
from shared.config import (
    RABBIT_HOST,
    RABBIT_PORT,
    RABBIT_USER,
    RABBIT_PASS,
    QUEUE_SCORED_POSTS as INPUT_QUEUE,
    QUEUE_TOPIC_POSTS as OUTPUT_QUEUE,
)

BATCH_SIZE = 32
BATCH_TIMEOUT = 1.0  # seconds

class TopicModel:
    def __init__(self):
        print("Initializing Keyword-Based Topic Classifier...")
        
        # Define high-signal topics and their keywords
        self.topics = {
            "Earnings & Guidance": [
                "earnings", "revenue", "guidance", "beat", "miss", "quarterly", 
                "report", "profit", "eps", "results", "fiscal", "outlook"
            ],
            "Fed & Macro": [
                "fed", "interest", "rates", "hike", "inflation", "cpi", "powell", 
                "fomc", "treasury", "yields", "economic", "recession", "gdp"
            ],
            "Technical Analysis": [
                "chart", "support", "resistance", "breakout", "rsi", "volume", 
                "indicator", "bullish", "bearish", "moving average", "macd", "patterns"
            ],
            "AI & Compute": [
                "ai", "llm", "compute", "gpu", "chips", "semiconductor", "nvda", 
                "amd", "blackwell", "training", "inference", "artificial intelligence"
            ],
            "Space & Satellite": [
                "space", "launch", "satellite", "rocket", "mission", "nasa", 
                "spacex", "asts", "rklb", "orbit", "payload", "starlink"
            ],
            "Management & Insider": [
                "ceo", "leadership", "insider", "buy", "sell", "board", 
                "shareholder", "meeting", "management", "founder", "stake"
            ],
            "M&A & Partnerships": [
                "merger", "acquisition", "buyout", "deal", "takeover", 
                "partnership", "collaboration", "joint venture", "contract"
            ],
            "Options & Volatility": [
                "options", "calls", "puts", "strike", "expiry", "delta", 
                "gamma", "premium", "yolo", "gambling", "vix", "volatility"
            ],
        }
        
        # Pre-compile regex for performance
        self.patterns = {
            label: re.compile(rf"\b({'|'.join(keywords)})\b", re.IGNORECASE)
            for label, keywords in self.topics.items()
        }
        
        print("Topic Classifier initialized.")

    def predict_batch(self, texts: list[str]) -> list[tuple[int, str]]:
        results = []
        for text in texts:
            matched_label = "General / Outlier"
            matched_id = -1
            
            # Simple priority-based matching
            # In a more advanced version, we could score multiple matches and pick the highest
            for i, (label, pattern) in enumerate(self.patterns.items()):
                if pattern.search(text):
                    matched_label = label
                    matched_id = i
                    break
            
            results.append((matched_id, matched_label))
            
        return results

    def predict(self, text: str):
        return self.predict_batch([text])[0]

    def predict(self, text: str):
        return self.predict_batch([text])[0]

def main():
    print("Starting Topic Service...")
    model = TopicModel()

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=INPUT_QUEUE, durable=True)
    channel.queue_declare(queue=OUTPUT_QUEUE, durable=True)

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
                if batch_posts:
                    print(f"Processing batch of {len(batch_posts)} posts...")
                    texts = [post.text for post in batch_posts]
                    results = model.predict_batch(texts)

                    for post, (topic_id, topic_label), method in zip(batch_posts, results, batch_methods):
                        try:
                            post.topic_id = topic_id
                            post.topic_label = topic_label
                            
                            channel.basic_publish(
                                exchange="",
                                routing_key=OUTPUT_QUEUE,
                                body=post.model_dump_json().encode(),
                                properties=pika.BasicProperties(delivery_mode=2),
                            )
                            print(f"{post.id}: Topic {topic_id} ({topic_label})")
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
