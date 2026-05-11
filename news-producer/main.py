import time
import json
import logging
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import feedparser
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
logger = logging.getLogger("news-producer")

# In container path
KEYWORDS_FILE = Path("/app/keywords.json")
POLL_INTERVAL = 60  # 1 minute

def get_symbols():
    try:
        # Try container path first, then fall back to local relative path for dev
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
    logger.info("Starting Yahoo Finance News Polling Producer...")
    
    symbols = get_symbols()
    logger.info(f"Tracking symbols for news: {symbols}")

    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    params = pika.ConnectionParameters(host=RABBIT_HOST, port=RABBIT_PORT, credentials=creds)
    
    # Track seen links to avoid duplicates
    seen_links = set()

    while True:
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_RAW_POSTS, durable=True)

            while True:
                for symbol in symbols:
                    try:
                        # Yahoo Finance RSS URL
                        url = f"https://finance.yahoo.com/rss/headline?s={symbol}"
                        feed = feedparser.parse(url)
                        
                        new_count = 0
                        for entry in feed.entries:
                            link = entry.link
                            if link in seen_links:
                                continue
                            
                            title = entry.get("title", "")
                            summary = entry.get("summary", "")
                            # Combine title and summary for better sentiment analysis
                            full_text = f"{title}. {summary}" if summary else title
                            
                            # Create a stable ID based on the link
                            post_id = f"news_{hashlib.md5(link.encode()).hexdigest()}"
                            
                            # Parse timestamp if available, else use now
                            try:
                                # feedparser usually normalizes this to a struct_time
                                dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                            except Exception:
                                dt = datetime.now(timezone.utc)

                            raw_post = RawPost(
                                id=post_id,
                                symbol=symbol,
                                platform="yahoo",
                                text=full_text,
                                timestamp=dt,
                            )

                            channel.basic_publish(
                                exchange="",
                                routing_key=QUEUE_RAW_POSTS,
                                body=raw_post.model_dump_json().encode(),
                                properties=pika.BasicProperties(delivery_mode=2),
                            )
                            
                            seen_links.add(link)
                            new_count += 1

                        if new_count > 0:
                            logger.info(f"Symbol '{symbol}': Published {new_count} new headlines.")
                        
                    except Exception as e:
                        logger.error(f"Error processing symbol {symbol}: {e}")
                    
                    time.sleep(1) # Small delay between symbols

                # Keep seen_links from growing infinitely (keep last 500)
                if len(seen_links) > 500:
                    seen_links = set(list(seen_links)[-500:])

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
