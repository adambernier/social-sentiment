import sys
import time
from pathlib import Path
import pika
import json
import pandas as pd
from bertopic import BERTopic
from umap import UMAP

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
        print("Initializing BERTopic model...")
        # In a real app, you'd load a pre-trained model:
        # self.topic_model = BERTopic.load("my_model")
        
        # For this demo, we'll initialize a basic BERTopic instance.
        # Since BERTopic needs to be 'fitted' to transform new documents,
        # we'll fit it on a few 'seed' financial topics if we were doing this for real.
        umap_model = UMAP(n_neighbors=2, n_components=2, min_dist=0.0, metric='cosine', random_state=42)
        self.topic_model = BERTopic(language="english", calculate_probabilities=False, umap_model=umap_model)
        
        # Mocking a fit for the demo so transform() doesn't fail.
        # In production, this service would load a model trained on historical data.
        dummy_data = [
            "Earnings report was better than expected for tech stocks",
            "Federal Reserve announces interest rate hike to combat inflation",
            "New product launch drives consumer interest in retail sector",
            "Management shakeup at major semiconductor firm",
            "Supply chain issues continue to plague automotive industry",
            "Bitcoin and crypto market experiencing high volatility",
            "Stock market rallies as jobs report exceeds expectations",
            "Oil prices drop amid global economic concerns",
            "New regulations could impact fintech growth",
            "AI startups see surge in venture capital funding",
            "Retail sales bounce back in latest quarterly report",
            "Housing market remains tight as interest rates climb",
            "Electric vehicle sales hit record highs this month",
            "Cloud computing revenue drives growth for tech giants",
            "Cybersecurity threats on the rise for financial institutions",
            "Green energy transition creates new investment opportunities",
            "Global trade tensions weigh on international markets",
            "Consumer confidence dips slightly in recent survey",
            "Semiconductor shortage continues to affect electronics production",
            "Dividends increased by several major blue-chip companies",
            "Merger and acquisition activity picks up in the healthcare sector",
            "Biotech firms announce promising results for new treatments",
            "Aerospace and defense stocks gain on new contracts",
            "Sustainable investing becomes a priority for many portfolios"
        ]
        self.topic_model.fit(dummy_data)
        print("BERTopic model initialized and dummy-fitted.")

    def predict_batch(self, texts: list[str]) -> list[tuple[int, str]]:
        topics, probs = self.topic_model.transform(texts)
        
        results = []
        for topic_id in topics:
            topic_id = int(topic_id)
            if topic_id != -1:
                info = self.topic_model.get_topic(topic_id)
                label = " ".join([word[0] for word in info[:3]])
            else:
                label = "General / Outlier"
            results.append((topic_id, label))
            
        return results

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
