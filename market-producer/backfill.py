import asyncio
import logging
import httpx
import aio_pika
import yfinance as yf
from datetime import datetime, timezone
import pandas as pd
import sys
from pathlib import Path

# Add root to sys.path for cross-service imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

import os
from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    QUEUE_RAW_POSTS,
    REDDIT_USER_AGENT,
)
from shared.schemas import RawPost, StockQuote
from storage_service.db import DB
from main import get_market_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("backfill")

async def backfill_reddit(symbols, channel):
    logger.info("Backfilling Reddit posts...")
    
    proxy_url = os.environ.get("REDDIT_PROXY_URL")
    if proxy_url:
        logger.info(f"Using configured Reddit proxy: {proxy_url[:15]}...")
    else:
        logger.warning("NO PROXY URL FOUND in environment!")
        
    # Use a normal browser user-agent to avoid Cloudflare blocks
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    async with httpx.AsyncClient(headers={"User-Agent": user_agent}, proxy=proxy_url) as client:
        for symbol in symbols:
            # We must use standard search to get historical posts for just this symbol
            url = f"https://www.reddit.com/search.json?q={symbol}&sort=new&limit=100"
            logger.info(f"Fetching Reddit history for {symbol}...")
            
            resp = await client.get(url, timeout=15)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch Reddit data for {symbol}: HTTP {resp.status_code}")
                continue
                
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            
            published_count = 0
            for child in children:
                post = child.get("data", {})
                post_id = post.get("id", "")
                if not post_id:
                    continue
                    
                body = post.get("selftext", "") or post.get("title", "")
                if not body:
                    continue
                
                # Verify exact symbol match in body/title
                import re
                if not re.search(rf"\b{re.escape(symbol)}\b", body, re.IGNORECASE):
                    continue
                    
                created_utc = post.get("created_utc")
                ts = datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else datetime.now(timezone.utc)
                engagement = max(1, int(post.get("score", 1)))
                
                raw_post = RawPost(
                    id=f"rd_hist_{post_id}",
                    symbol=symbol,
                    platform="reddit",
                    text=body.strip()[:2000],
                    timestamp=ts,
                    engagement=engagement,
                )
                
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=raw_post.model_dump_json().encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=QUEUE_RAW_POSTS,
                )
                published_count += 1
                
            logger.info(f"Pushed {published_count} historical Reddit posts for {symbol} to queue.")
            await asyncio.sleep(2) # be nice to Reddit

async def backfill_market(db, symbols):
    logger.info("Backfilling Market Data (30d hourly)...")
    for symbol in symbols:
        try:
            logger.info(f"Fetching yfinance history for {symbol}...")
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="1mo", interval="1h")
            
            if df.empty:
                logger.warning(f"No history found for {symbol}")
                continue
                
            inserted_count = 0
            for index, row in df.iterrows():
                # index is pandas Timestamp
                ts = index.to_pydatetime().astimezone(timezone.utc)
                session = get_market_session(ts)
                
                quote = StockQuote(
                    symbol=symbol,
                    timestamp=ts,
                    price=float(row["Close"]),
                    volume=int(row["Volume"]),
                    market_session=session
                )
                if db.insert_quote(quote):
                    inserted_count += 1
                    
            logger.info(f"Inserted {inserted_count} historical quotes for {symbol}.")
            
        except Exception as e:
            logger.error(f"Error fetching market data for {symbol}: {e}")

async def main():
    db = DB()
    
    import sys
    if len(sys.argv) > 1:
        symbols = [sys.argv[1].upper()]
    else:
        with db.conn.cursor() as cur:
            cur.execute("SELECT symbol FROM tracked_symbols WHERE is_active = true")
            symbols = [row[0] for row in cur.fetchall()]
            
    if not symbols:
        logger.warning("No active symbols found in tracked_symbols table.")
        return
        
    logger.info(f"Backfilling data for symbols: {symbols}")
    
    # 1. Backfill Market Data
    await backfill_market(db, symbols)
    
    # 2. Backfill Reddit Data
    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    connection = await aio_pika.connect_robust(rabbit_url)
    async with connection:
        channel = await connection.channel()
        await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)
        await backfill_reddit(symbols, channel)
        
    logger.info("Backfill complete! Check Grafana Internal Pipeline metrics as the ML models process the queue.")

if __name__ == "__main__":
    asyncio.run(main())
