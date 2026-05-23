import time
import sys
import random
import string
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

BULLISH_PHRASES = [
    "INTC is looking extremely bullish here!",
    "Just loaded up more Intel shares. The turnaround is real.",
    "INTC technicals are breaking out. Target $150.",
    "Intel's new chip architecture is a game changer. Buying more.",
    "INTC undervalued? I think so. Bullish.",
    "Massive buy walls appearing on $INTC.",
    "Intel Foundry news is going to send this to the moon.",
    "I'm all in on Intel for the 2026 recovery.",
    "Earnings beat incoming for INTC. Stay long.",
    "Shorts are about to get squeezed on Intel."
]

BEARISH_PHRASES = [
    "INTC is a value trap. Avoid.",
    "Don't fall for the Intel bounce, it's a dead cat.",
    "Intel's market share is being eaten by AMD. Selling my position.",
    "INTC earnings were a disaster. Bearish.",
    "Just shorted INTC. This thing is going to $100.",
    "Intel is the new IBM. Stagnant and dying.",
    "Foundry business is burning too much cash. Exit INTC.",
    "AMD and NVIDIA are lightyears ahead of Intel.",
    "Technical breakdown on the $INTC monthly chart.",
    "Institutional selling seen in Intel today. Watch out."
]

def main():
    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    channel.queue_declare(queue=INPUT_QUEUE, durable=True)

    print("🚀 Live Simulator Started!")
    print("Pushing a unique INTC post every 10 seconds. Watch your dashboard!")
    
    try:
        count = 0
        while True:
            is_bullish = random.random() > 0.4 # Slightly bullish bias
            base_text = random.choice(BULLISH_PHRASES if is_bullish else BEARISH_PHRASES)
            
            # Add a random unique suffix to ensure the text looks different in the feed
            suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
            emoji = " 🚀" if is_bullish else " 📉"
            text = f"{base_text} [{suffix}]{emoji}"
            
            post_id = f"sim_{int(time.time())}_{suffix}"
            
            post = RawPost(
                id=post_id,
                symbol="INTC", # Updated to include symbol
                platform="simulator",
                text=text,
                timestamp=datetime.now(timezone.utc),
                engagement=random.randint(1, 50),
            )
            
            channel.basic_publish(
                exchange="",
                routing_key=INPUT_QUEUE,
                body=post.model_dump_json().encode(),
                properties=pika.BasicProperties(delivery_mode=2),
            )
            
            count += 1
            print(f"[{count}] Sent: {text}")
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
    finally:
        connection.close()

if __name__ == "__main__":
    main()
