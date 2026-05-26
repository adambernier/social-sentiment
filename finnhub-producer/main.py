import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import aio_pika

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    QUEUE_RAW_POSTS,
    FINNHUB_API_KEY,
    get_env_int,
)
from shared.schemas import RawPost
from shared.symbols import tickers, match_symbol
from shared.pacing import AsyncRateLimiter, paced_gather
from shared.metrics import start_metrics_server, POSTS_INGESTED_TOTAL, RATE_LIMITS_HIT_TOTAL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("finnhub-producer")
# httpx logs each request URL at INFO, which includes the ?token=... API key.
# Quiet it so the key never lands in the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Finnhub's free tier allows ~60 req/min; 10 symbols every 15 min is comfortable.
# News is slow-moving, so a long interval also keeps duplicate re-fetches low.
POLL_INTERVAL = get_env_int("FINNHUB_POLL_INTERVAL", 900)
# Exponential backoff cap when rate-limited, so we stop hammering and recover.
MAX_BACKOFF = get_env_int("FINNHUB_MAX_BACKOFF", 3600)
# Days of history requested each poll; dedup keeps re-fetched articles out, so this
# only controls how far back we'd pick up anything missed during downtime.
LOOKBACK_DAYS = get_env_int("FINNHUB_LOOKBACK_DAYS", 3)
# Request shaping: cap in-flight requests and pace new ones. The free tier is
# ~60 req/min, so default RATE_PER_MIN stays just under that; concurrency keeps
# the per-cycle burst small even as the symbol list grows.
MAX_CONCURRENCY = get_env_int("FINNHUB_MAX_CONCURRENCY", 4)
RATE_PER_MIN = get_env_int("FINNHUB_RATE_PER_MIN", 55)
BASE_URL = "https://finnhub.io/api/v1/company-news"


async def fetch_symbol_news(symbol: str, client: httpx.AsyncClient, channel: aio_pika.Channel, seen_ids: set) -> bool:
    """Fetch and publish recent Finnhub company news for one symbol.

    Returns True if the request was rate-limited (HTTP 429) so the caller can
    back off; False otherwise.
    """
    try:
        today = datetime.now(timezone.utc).date()
        params = {
            "symbol": symbol,
            "from": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
            "to": today.isoformat(),
            "token": FINNHUB_API_KEY,
        }
        response = await client.get(BASE_URL, params=params)
        if response.status_code == 429:
            logger.warning(f"Rate limited (429) fetching news for {symbol}")
            return True
        if response.status_code != 200:
            logger.error(f"Error fetching news for {symbol}: {response.status_code}")
            return False

        articles = response.json()
        if not isinstance(articles, list):
            logger.error(f"Unexpected response for {symbol}: {articles!r:.200}")
            return False

        new_count = 0
        skipped = 0
        for art in articles:
            art_id = art.get("id")
            if art_id is None:
                continue
            post_id = f"finnhub_{art_id}"
            if post_id in seen_ids:
                continue

            headline = (art.get("headline") or "").strip()
            summary = (art.get("summary") or "").strip()
            full_text = f"{headline}. {summary}" if summary else headline
            if not full_text.strip():
                continue

            # Finnhub's per-symbol feed is broad — lots of generic market/ETF
            # content that merely co-occurs with the ticker. Keep only articles that
            # actually name the company, using the same high-precision matcher the
            # social producers rely on. Mark skipped ids seen so we don't recheck
            # them every poll.
            if not match_symbol(full_text, symbol):
                seen_ids.add(post_id)
                skipped += 1
                continue

            try:
                dt = datetime.fromtimestamp(int(art["datetime"]), tz=timezone.utc)
            except (KeyError, TypeError, ValueError):
                dt = datetime.now(timezone.utc)

            raw_post = RawPost(
                id=post_id,
                symbol=symbol,
                platform="finnhub",
                text=full_text,
                timestamp=dt,
                engagement=15,
            )

            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=raw_post.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=QUEUE_RAW_POSTS,
            )

            POSTS_INGESTED_TOTAL.labels(platform="finnhub", symbol=symbol).inc()
            seen_ids.add(post_id)
            new_count += 1

        if new_count or skipped:
            logger.info(f"Symbol '{symbol}': published {new_count}, skipped {skipped} off-topic.")
        return False

    except Exception as e:
        logger.error(f"Error processing symbol {symbol}: {e}")
        return False


async def main():
    logger.info("Starting Finnhub Company News Producer...")
    start_metrics_server(8002)

    if not FINNHUB_API_KEY:
        logger.error(
            "FINNHUB_API_KEY is not set; cannot fetch news. Get a free key at "
            "https://finnhub.io and set FINNHUB_API_KEY in the environment. Idling."
        )
        # Idle rather than crash-loop, so the container stays up without hammering.
        while True:
            await asyncio.sleep(3600)

    logger.info(f"Tracking symbols: {tickers()}")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"
    seen_ids = set()

    # One limiter for the lifetime of the process (survives reconnects below).
    limiter = AsyncRateLimiter(max_rate=RATE_PER_MIN, period=60.0)

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                async with httpx.AsyncClient(timeout=10) as client:
                    backoff = POLL_INTERVAL
                    while True:
                        # Re-read each poll so runtime symbol additions are honored
                        # without a restart (shared.symbols refreshes from the DB).
                        symbols = tickers()
                        results = await paced_gather(
                            symbols,
                            lambda sym: fetch_symbol_news(sym, client, channel, seen_ids),
                            max_concurrency=MAX_CONCURRENCY,
                            limiter=limiter,
                        )
                        rate_limited = any(results)

                        # Bound the dedup set (Finnhub returns far more articles than
                        # Yahoo RSS, so keep a larger rolling window).
                        if len(seen_ids) > 2000:
                            seen_ids = set(list(seen_ids)[-2000:])

                        if rate_limited:
                            RATE_LIMITS_HIT_TOTAL.labels(platform="finnhub").inc()
                            backoff = min(backoff * 2, MAX_BACKOFF)
                            logger.warning(
                                f"Rate limited by Finnhub (429). Backing off for {backoff}s."
                            )
                        else:
                            if backoff != POLL_INTERVAL:
                                logger.info("Requests succeeding again; resetting poll interval.")
                            backoff = POLL_INTERVAL

                        logger.info(f"Batch complete. Sleeping for {backoff} seconds...")
                        await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"Error in main loop: {e}. Retrying in 10s...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    from shared.runtime import run
    run(main, name="finnhub-producer")
