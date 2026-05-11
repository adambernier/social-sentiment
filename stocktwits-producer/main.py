import time
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pika

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    QUEUE_RAW_POSTS,
)
from shared.schemas import RawPost

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("stocktwits-producer")

# In container path
KEYWORDS_FILE = Path("/app/keywords.json")
POLL_INTERVAL = 60  # 1 minute

def get_symbols():
    try:
        path = KEYWORDS_FILE if KEYWORDS_FILE.exists() else Path(__file__).parent.parent / "bluesky-producer" / "keywords.json"
        if path.exists():
            with open(path, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return list(data.keys())
                return data
        return ["ASTS", "RKLB", "INTC"]
    except Exception as e:
        logger.error(f"Error reading keywords: {e}")
        return ["ASTS", "RKLB", "INTC"]

def main():
    logger.info("Starting StockTwits Polling Producer...")
    
    symbols = get_symbols()
    logger.info(f"Tracking symbols: {symbols}")

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    
    # Track latest message ID per symbol to avoid duplicates
    last_seen_ids = {symbol: 0 for symbol in symbols}

    while True:
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_RAW_POSTS, durable=True)

            while True:
                for symbol in symbols:
                    try:
                        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
                        params_api = {}
                        if last_seen_ids[symbol] > 0:
                            params_api["since"] = last_seen_ids[symbol]

                        response = httpx.get(url, params=params_api, timeout=10)
                        if response.status_code != 200:
                            logger.error(f"Error fetching {symbol} from StockTwits: {response.status_code}")
                            continue

                        data = response.json()
                        messages = data.get("messages", [])
                        
                        new_count = 0
                        for msg in reversed(messages):  # Process oldest to newest
                            msg_id = str(msg["id"])
                            body = msg["body"]
                            created_at_str = msg["created_at"]
                            
                            # Parse StockTwits timestamp (ISO8601 usually)
                            # Example: "2026-05-09T17:00:00Z"
                            try:
                                timestamp = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                            except Exception:
                                timestamp = datetime.now(timezone.utc)

                            raw_post = RawPost(
                                id=f"st_{msg_id}",
                                symbol=symbol,
                                platform="stocktwits",
                                text=body,
                                timestamp=timestamp,
                            )

                            channel.basic_publish(
                                exchange="",
                                routing_key=QUEUE_RAW_POSTS,
                                body=raw_post.model_dump_json().encode(),
                                properties=pika.BasicProperties(delivery_mode=2),
                            )
                            
                            new_count += 1
                            if msg["id"] > last_seen_ids[symbol]:
                                last_seen_ids[symbol] = msg["id"]

                        if new_count > 0:
                            logger.info(f"Symbol '{symbol}': Published {new_count} new messages.")
                        
                    except Exception as e:
                        logger.error(f"Error processing symbol {symbol}: {e}")
                    
                    # Sleep briefly between symbols to avoid aggressive bursts
                    time.sleep(2)

                logger.info(f"Batch complete. Sleeping for {POLL_INTERVAL} seconds...")
                time.sleep(POLL_INTERVAL)

        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in 10s...")
            time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Service stopped.")
