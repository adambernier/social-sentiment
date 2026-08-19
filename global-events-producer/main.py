"""Poll explicit per-stock geopolitical rules and store normalized signals."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

try:
    from .adapter import (
        EventProviderRateLimited,
        EventRule,
        GdeltEventAdapter,
        GlobalEventAdapter,
        NormalizedEventSignal,
        build_gdelt_query,
    )
except ImportError:  # Running as ``python global-events-producer/main.py``.
    from adapter import (
        EventProviderRateLimited,
        EventRule,
        GdeltEventAdapter,
        GlobalEventAdapter,
        NormalizedEventSignal,
        build_gdelt_query,
    )
from shared.config import DATABASE_DSN, GLOBAL_CONTEXT_ENABLED, get_env_int
from shared.metrics import (
    GLOBAL_LAST_SUCCESS_TIMESTAMP,
    GLOBAL_PROVIDER_REQUESTS_TOTAL,
    GLOBAL_RULE_MATCHES_TOTAL,
    RATE_LIMITS_HIT_TOTAL,
    initialize_rate_limit_metrics,
    start_metrics_server,
)
from shared.pacing import AsyncRateLimiter

logger = logging.getLogger("global-events-producer")

POLL_INTERVAL_SECONDS = get_env_int("GLOBAL_EVENTS_POLL_SECONDS", 15 * 60)
LOOKBACK_HOURS = get_env_int("GLOBAL_EVENTS_LOOKBACK_HOURS", 48)
MAX_PER_RULE_PER_DAY = get_env_int("GLOBAL_EVENTS_MAX_PER_RULE_DAY", 20)
RATE_PER_MINUTE = get_env_int("GLOBAL_EVENTS_RATE_PER_MINUTE", 10)


async def load_rules(conn: psycopg.AsyncConnection) -> list[EventRule]:
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT rule.id, rule.symbol, rule.name, rule.countries,
                   rule.themes, rule.query_terms
            FROM global_event_rules AS rule
            JOIN tracked_symbols AS stock ON stock.symbol = rule.symbol
            WHERE rule.is_active AND stock.is_active
            ORDER BY rule.symbol, rule.id
            """
        )
        rows = await cursor.fetchall()
    return [
        EventRule(
            id=row["id"],
            symbol=row["symbol"],
            name=row["name"],
            countries=tuple(row["countries"]),
            themes=tuple(row["themes"]),
            query_terms=tuple(row["query_terms"]),
        )
        for row in rows
    ]


async def _existing_daily_counts(
    conn: psycopg.AsyncConnection,
    rule_id: int,
    events: list[NormalizedEventSignal],
) -> dict:
    days = sorted({event.occurred_at.date() for event in events})
    if not days:
        return {}
    async with conn.cursor() as cursor:
        await cursor.execute(
            """
            SELECT (signal.occurred_at AT TIME ZONE 'UTC')::date, COUNT(*)
            FROM stock_event_links AS link
            JOIN global_event_signals AS signal ON signal.id = link.event_id
            WHERE link.rule_id = %s
              AND (signal.occurred_at AT TIME ZONE 'UTC')::date = ANY(%s)
            GROUP BY (signal.occurred_at AT TIME ZONE 'UTC')::date
            """,
            [rule_id, days],
        )
        return dict(await cursor.fetchall())


async def store_rule_events(
    conn: psycopg.AsyncConnection,
    rule: EventRule,
    events: list[NormalizedEventSignal],
    *,
    cap_per_day: int = MAX_PER_RULE_PER_DAY,
) -> int:
    """Deduplicate and link signals atomically, retaining the exact rule reason."""
    if cap_per_day <= 0 or not events:
        return 0
    daily_counts = await _existing_daily_counts(conn, rule.id, events)
    linked = 0
    match_reason = json.dumps(
        {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "countries": list(rule.countries),
            "themes": list(rule.themes),
            "query_terms": list(rule.query_terms),
            "provider_query": build_gdelt_query(rule),
        }
    )

    async with conn.transaction(), conn.cursor() as cursor:
        for event in sorted(events, key=lambda item: item.occurred_at):
            event_day = event.occurred_at.date()
            if daily_counts.get(event_day, 0) >= cap_per_day:
                continue
            await cursor.execute(
                """
                    INSERT INTO global_event_signals (
                        provider, provider_event_id, canonical_url, title,
                        summary, source_name, occurred_at, countries, themes
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                [
                    event.provider,
                    event.provider_event_id,
                    event.canonical_url,
                    event.title,
                    event.summary,
                    event.source_name,
                    event.occurred_at,
                    json.dumps(event.countries),
                    json.dumps(event.themes),
                ],
            )
            inserted = await cursor.fetchone()
            if inserted is None:
                await cursor.execute(
                    """
                    SELECT id
                    FROM global_event_signals
                    WHERE provider = %s
                      AND (
                          provider_event_id = %s
                          OR canonical_url = %s
                      )
                    ORDER BY
                        CASE WHEN provider_event_id = %s THEN 0 ELSE 1 END,
                        id
                    LIMIT 1
                    """,
                    [
                        event.provider,
                        event.provider_event_id,
                        event.canonical_url,
                        event.provider_event_id,
                    ],
                )
                existing = await cursor.fetchone()
                if existing is None:
                    raise RuntimeError("event deduplication conflict was not found")
                event_id = existing[0]
                await cursor.execute(
                    """
                    UPDATE global_event_signals
                    SET title = %s,
                        summary = %s,
                        source_name = %s,
                        occurred_at = %s,
                        countries = %s::jsonb,
                        themes = %s::jsonb,
                        ingested_at = NOW()
                    WHERE id = %s
                    """,
                    [
                        event.title,
                        event.summary,
                        event.source_name,
                        event.occurred_at,
                        json.dumps(event.countries),
                        json.dumps(event.themes),
                        event_id,
                    ],
                )
            else:
                event_id = inserted[0]
            await cursor.execute(
                """
                    INSERT INTO stock_event_links (
                        event_id, symbol, rule_id, match_reason
                    )
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (event_id, symbol, rule_id) DO NOTHING
                    RETURNING event_id
                    """,
                [event_id, rule.symbol, rule.id, match_reason],
            )
            if await cursor.fetchone() is not None:
                linked += 1
                daily_counts[event_day] = daily_counts.get(event_day, 0) + 1
    return linked


async def poll_once(
    conn: psycopg.AsyncConnection,
    adapter: GlobalEventAdapter,
    limiter: AsyncRateLimiter,
    *,
    now_utc: datetime,
) -> int:
    rules = await load_rules(conn)
    total_linked = 0
    for rule in rules:
        await limiter.acquire()
        try:
            events = await adapter.fetch(
                rule,
                since=now_utc - timedelta(hours=LOOKBACK_HOURS),
                max_results=MAX_PER_RULE_PER_DAY,
            )
            linked = await store_rule_events(conn, rule, events)
            total_linked += linked
            GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
                provider=adapter.provider_name,
                data_type="events",
                status="success" if events else "no_data",
            ).inc()
            if linked:
                GLOBAL_RULE_MATCHES_TOTAL.labels(
                    provider=adapter.provider_name,
                    symbol=rule.symbol,
                ).inc(linked)
                latest = max(event.occurred_at for event in events)
                GLOBAL_LAST_SUCCESS_TIMESTAMP.labels(
                    provider=adapter.provider_name,
                    data_type="events",
                ).set(latest.timestamp())
        except EventProviderRateLimited as error:
            GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
                provider=adapter.provider_name,
                data_type="events",
                status="rate_limited",
            ).inc()
            RATE_LIMITS_HIT_TOTAL.labels(platform="global-events").inc()
            logger.warning(
                "GDELT rate limited rule %s; retry-after=%s",
                rule.id,
                error.retry_after_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            GLOBAL_PROVIDER_REQUESTS_TOTAL.labels(
                provider=adapter.provider_name,
                data_type="events",
                status="error",
            ).inc()
            logger.exception(
                "Event polling failed for rule %s (%s)",
                rule.id,
                rule.name,
            )
    return total_linked


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    initialize_rate_limit_metrics("global-events")
    start_metrics_server(8010)
    if not GLOBAL_CONTEXT_ENABLED:
        logger.info("Global context is disabled; event ingestion is idle")
        await asyncio.Event().wait()
        return

    limiter = AsyncRateLimiter(max_rate=RATE_PER_MINUTE, period=60.0)
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with (
        httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "social-sentiment-global-context/1.0"},
        ) as client,
        await psycopg.AsyncConnection.connect(
            DATABASE_DSN,
            autocommit=True,
        ) as conn,
    ):
        adapter = GdeltEventAdapter(client)
        while True:
            try:
                linked = await poll_once(
                    conn,
                    adapter,
                    limiter,
                    now_utc=datetime.now(timezone.utc),
                )
                logger.info("Global event poll linked %s new signals", linked)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Global event poll cycle failed; retrying next interval"
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    from shared.runtime import run

    run(main, name="global-events-producer")
