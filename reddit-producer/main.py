import asyncio
import logging
import os
import random
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aio_pika
import httpx

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import (
    QUEUE_RAW_POSTS,
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
    REDDIT_USER_AGENT,
    get_env_int,
)
from shared.metrics import (
    POSTS_INGESTED_TOTAL,
    RATE_LIMITS_HIT_TOTAL,
    initialize_rate_limit_metrics,
    start_metrics_server,
)
from shared.polling import PollOutcome, PollStatus
from shared.schemas import RawPost
from shared.symbols import keywords_map, match_symbol, run_with_symbol_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("reddit-producer")

# Reddit JSON multireddit comments feed
SUBREDDITS = (
    "wallstreetbets+stocks+investing+SecurityAnalysis"
    "+options+StockMarket+semiconductors+Spacestocks"
)
FEED_URL = f"https://www.reddit.com/r/{SUBREDDITS}/comments.json?limit=100"
POLL_INTERVAL = get_env_int("REDDIT_POLL_INTERVAL", 900)
MAX_BACKOFF = get_env_int("REDDIT_MAX_BACKOFF", 3600)
TRANSIENT_RETRY_INTERVAL = get_env_int("REDDIT_TRANSIENT_RETRY_INTERVAL", 60)
TRANSIENT_RETRY_JITTER = get_env_int("REDDIT_TRANSIENT_RETRY_JITTER", 15)

# Global deque for ID-based deduplication
seen_ids = deque(maxlen=2000)


def parse_retry_after(value: str | None) -> int | None:
    try:
        seconds = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def next_poll_delay(
    outcome: PollOutcome,
    current_backoff: float,
    *,
    jitter_seconds: float | None = None,
) -> float:
    if outcome.status is PollStatus.RATE_LIMITED:
        if outcome.retry_after_seconds is not None:
            return min(float(outcome.retry_after_seconds), float(MAX_BACKOFF))
        return min(current_backoff * 2, float(MAX_BACKOFF))

    if outcome.status is PollStatus.BLOCKED:
        return min(current_backoff * 2, float(MAX_BACKOFF))

    if outcome.status is PollStatus.TRANSIENT_ERROR:
        retry_interval = min(
            float(POLL_INTERVAL),
            max(1.0, float(TRANSIENT_RETRY_INTERVAL)),
        )
        max_jitter = min(
            max(0.0, float(TRANSIENT_RETRY_JITTER)),
            max(0.0, float(POLL_INTERVAL) - retry_interval),
        )
        jitter = (
            random.uniform(0.0, max_jitter)
            if jitter_seconds is None
            else min(max(0.0, jitter_seconds), max_jitter)
        )
        return retry_interval + jitter

    return float(POLL_INTERVAL)


async def fetch_and_process(
    client: httpx.AsyncClient,
    channel: aio_pika.Channel,
    processed_ids: deque[str] | None = None,
) -> PollOutcome:
    if processed_ids is None:
        processed_ids = seen_ids

    try:
        logger.info(f"Fetching RSS feed: {FEED_URL}")
        resp = await client.get(FEED_URL, timeout=15)
    except httpx.RequestError as error:
        logger.error("Transient Reddit request error: %s", error)
        return PollOutcome(PollStatus.TRANSIENT_ERROR)

    if resp.status_code == 403:
        logger.error(
            "Reddit 403 blocked (unauthenticated JSON or flagged IP); "
            "escalating blocked-provider backoff."
        )
        return PollOutcome(PollStatus.BLOCKED)
    if resp.status_code == 429:
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        logger.warning(
            "Reddit 429 rate limited; Retry-After=%s",
            retry_after if retry_after is not None else "unavailable",
        )
        return PollOutcome(PollStatus.RATE_LIMITED, retry_after)
    if 500 <= resp.status_code < 600:
        logger.error("Transient Reddit server error: HTTP %s", resp.status_code)
        return PollOutcome(PollStatus.TRANSIENT_ERROR)
    if resp.status_code != 200:
        logger.error("Reddit request failed: HTTP %s", resp.status_code)
        return PollOutcome(PollStatus.ERROR)

    try:
        data = resp.json()
    except (TypeError, ValueError) as error:
        logger.error("Invalid Reddit JSON response: %s", error)
        return PollOutcome(PollStatus.TRANSIENT_ERROR)

    if not isinstance(data, dict):
        logger.error("Invalid Reddit response root: expected an object")
        return PollOutcome(PollStatus.TRANSIENT_ERROR)
    response_data = data.get("data", {})
    if not isinstance(response_data, dict):
        logger.error("Invalid Reddit response data: expected an object")
        return PollOutcome(PollStatus.TRANSIENT_ERROR)
    children = response_data.get("children", [])
    if not isinstance(children, list):
        logger.error("Invalid Reddit response children: expected a list")
        return PollOutcome(PollStatus.TRANSIENT_ERROR)

    kw_map = keywords_map()
    new_posts_count = 0
    total_entries = len(children)
    unseen_count = 0
    matched_symbols = set()

    for child in children:
        if not isinstance(child, dict):
            continue
        comment = child.get("data", {})
        if not isinstance(comment, dict):
            continue

        raw_post_id = comment.get("id", "")
        if not isinstance(raw_post_id, (str, int)) or not raw_post_id:
            continue
        post_id = str(raw_post_id)
        if post_id in processed_ids:
            continue

        unseen_count += 1
        body = comment.get("body", "")
        if not isinstance(body, str) or not body:
            processed_ids.append(post_id)
            continue

        created_utc = comment.get("created_utc")
        try:
            timestamp = (
                datetime.fromtimestamp(created_utc, tz=timezone.utc)
                if created_utc is not None
                else datetime.now(timezone.utc)
            )
        except (OSError, OverflowError, TypeError, ValueError):
            timestamp = datetime.now(timezone.utc)

        try:
            engagement = max(1, int(comment.get("score", 1)))
        except (TypeError, ValueError):
            engagement = 1

        for symbol in kw_map:
            if not match_symbol(body, symbol):
                continue

            raw_post = RawPost(
                id=f"rd_{post_id}",
                symbol=symbol,
                platform="reddit",
                text=body.strip()[:2000],
                timestamp=timestamp,
                engagement=engagement,
            )
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=raw_post.model_dump_json().encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=QUEUE_RAW_POSTS,
            )
            POSTS_INGESTED_TOTAL.labels(platform="reddit", symbol=symbol).inc()
            new_posts_count += 1
            matched_symbols.add(symbol)

        # Advance only after every matching publication succeeds. A broker
        # failure leaves the ID eligible for retry after reconnection.
        processed_ids.append(post_id)

    logger.info(
        "Cycle complete. Published %d new Reddit comments across %d symbols. "
        "(Feed: %d, New: %d)",
        new_posts_count,
        len(matched_symbols),
        total_entries,
        unseen_count,
    )
    return PollOutcome(PollStatus.SUCCESS)


async def main():
    logger.info("Starting Reddit RSS Producer...")
    initialize_rate_limit_metrics("reddit")
    start_metrics_server(8005)
    
    # Initialize seen_ids with a "drain" fetch to avoid re-publishing old posts on startup
    # We do one quick fetch to populate the deque.
    proxy_url = os.environ.get("REDDIT_PROXY_URL")
    async with httpx.AsyncClient(headers={"User-Agent": REDDIT_USER_AGENT}, proxy=proxy_url) as client:
        try:
            logger.info("Performing startup drain fetch to populate seen_ids...")
            resp = await client.get(FEED_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                children = data.get("data", {}).get("children", [])
                for child in children:
                    pid = child.get("data", {}).get("id", "")
                    if pid:
                        seen_ids.append(pid)
            logger.info(f"Startup drain complete. Seeded {len(seen_ids)} IDs.")
        except Exception as e:
            logger.warning(f"Startup drain failed: {e}. Will proceed anyway.")

    rabbit_url = f"amqp://{RABBIT_USER}:{RABBIT_PASS}@{RABBIT_HOST}:{RABBIT_PORT}/"

    while True:
        try:
            connection = await aio_pika.connect_robust(rabbit_url)
            async with connection:
                channel = await connection.channel()
                await channel.declare_queue(QUEUE_RAW_POSTS, durable=True)

                proxy_url = os.environ.get("REDDIT_PROXY_URL")
                async with httpx.AsyncClient(headers={"User-Agent": REDDIT_USER_AGENT}, proxy=proxy_url) as client:
                    backoff = POLL_INTERVAL
                    while True:
                        outcome = await fetch_and_process(client, channel)
                        previous_backoff = backoff

                        if outcome.status is PollStatus.RATE_LIMITED:
                            RATE_LIMITS_HIT_TOTAL.labels(platform="reddit").inc()
                        backoff = next_poll_delay(outcome, backoff)

                        if outcome.status is PollStatus.RATE_LIMITED:
                            logger.warning(
                                "Rate limited by Reddit. Backing off for %.1fs.",
                                backoff,
                            )
                        elif outcome.status is PollStatus.BLOCKED:
                            logger.warning(
                                "Reddit access is blocked. Backing off for %.1fs.",
                                backoff,
                            )
                        elif outcome.status is PollStatus.TRANSIENT_ERROR:
                            logger.warning(
                                "Transient Reddit failure. Retrying in %.1fs.",
                                backoff,
                            )
                        elif (
                            outcome.status is PollStatus.SUCCESS
                            and previous_backoff != POLL_INTERVAL
                        ):
                            logger.info(
                                "Requests succeeding again; resetting poll interval."
                            )

                        logger.info(
                            "Batch complete. Sleeping for %.1f seconds...",
                            backoff,
                        )
                        await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(
                "Reddit producer connection or publish error: %s. Retrying in 10s...",
                e,
            )
            await asyncio.sleep(10)


async def service_main():
    await run_with_symbol_registry(main)


if __name__ == "__main__":
    from shared.runtime import run
    run(service_main, name="reddit-producer")
