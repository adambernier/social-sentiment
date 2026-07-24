import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import DATABASE_DSN
from shared.global_context import NormalizedMarketBar
from shared.global_instrument_catalog import CatalogInstrument
from shared.schemas import ScoredPost, StockMetrics, StockQuote

# Serializes post-retention maintenance across scheduler/manual invocations. The
# transaction-scoped lock is acquired inside the atomic move statement and is
# released as soon as that statement commits or rolls back.
POST_RETENTION_ADVISORY_LOCK_KEY = 4815162343

INSERT_POST_SQL = """
    INSERT INTO posts (id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, engagement)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (platform, id, symbol) DO NOTHING
    RETURNING id
"""

INSERT_QUOTE_SQL = """
    INSERT INTO stock_quotes (symbol, timestamp, price, volume, market_session)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (symbol, timestamp) DO NOTHING
    RETURNING id
"""

UPSERT_METRICS_SQL = """
    INSERT INTO stock_metrics (
        symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
        pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (symbol) DO UPDATE SET
        pe_ratio = EXCLUDED.pe_ratio,
        beta = EXCLUDED.beta,
        avg_return_1y = EXCLUDED.avg_return_1y,
        inflation_adj_return_1y = EXCLUDED.inflation_adj_return_1y,
        pe_relative_sector = EXCLUDED.pe_relative_sector,
        beta_relative_sector = EXCLUDED.beta_relative_sector,
        return_relative_sector = EXCLUDED.return_relative_sector,
        updated_at = NOW()
    RETURNING symbol
"""

UPSERT_GLOBAL_BAR_SQL = """
    INSERT INTO global_market_bars (
        instrument_key, interval, starts_at, ends_at, session_date,
        open_price, high_price, low_price, close_price, volume, provider,
        fetched_at
    )
    VALUES (
        %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s,
        NOW()
    )
    ON CONFLICT (instrument_key, interval, starts_at) DO UPDATE SET
        ends_at = EXCLUDED.ends_at,
        session_date = EXCLUDED.session_date,
        open_price = EXCLUDED.open_price,
        high_price = EXCLUDED.high_price,
        low_price = EXCLUDED.low_price,
        close_price = EXCLUDED.close_price,
        volume = EXCLUDED.volume,
        provider = EXCLUDED.provider,
        fetched_at = NOW()
"""

UPSERT_GLOBAL_INSTRUMENT_SQL = """
    INSERT INTO global_instruments (
        instrument_key, display_name, asset_class, currency, exchange,
        timezone, provider_aliases, session_metadata, quote_convention,
        is_active
    )
    VALUES (
        %s, %s, %s, %s, %s,
        %s, %s::jsonb, %s::jsonb, %s,
        %s
    )
    ON CONFLICT (instrument_key) DO UPDATE SET
        display_name = EXCLUDED.display_name,
        asset_class = EXCLUDED.asset_class,
        currency = EXCLUDED.currency,
        exchange = EXCLUDED.exchange,
        timezone = EXCLUDED.timezone,
        provider_aliases = EXCLUDED.provider_aliases,
        session_metadata = EXCLUDED.session_metadata,
        quote_convention = EXCLUDED.quote_convention,
        is_active = EXCLUDED.is_active,
        updated_at = NOW()
"""

ROLLUP_AND_PRUNE_POSTS_SQL = """
    WITH maintenance_lock AS MATERIALIZED (
        SELECT pg_advisory_xact_lock(%s)
    ),
    deleted_posts AS (
        DELETE FROM posts
        WHERE timestamp < %s
          AND EXISTS (SELECT 1 FROM maintenance_lock)
        RETURNING symbol, timestamp, sentiment, engagement
    ),
    aggregated_posts AS (
        SELECT
            symbol,
            date_trunc('hour', timestamp, 'UTC') AS bucket_hour,
            COUNT(*) FILTER (
                WHERE sentiment = 'positive'
            ) AS positive_count,
            COUNT(*) FILTER (
                WHERE sentiment = 'neutral'
            ) AS neutral_count,
            COUNT(*) FILTER (
                WHERE sentiment = 'negative'
            ) AS negative_count,
            COALESCE(SUM(LN(engagement + 1.0)) FILTER (
                WHERE sentiment = 'positive'
            ), 0) AS positive_weighted,
            COALESCE(SUM(LN(engagement + 1.0)) FILTER (
                WHERE sentiment = 'negative'
            ), 0) AS negative_weighted,
            COALESCE(SUM(LN(engagement + 1.0)) FILTER (
                WHERE sentiment = 'neutral'
            ), 0) AS neutral_weighted,
            COALESCE(SUM(LN(engagement + 1.0)), 0) AS total_weighted
        FROM deleted_posts
        GROUP BY symbol, date_trunc('hour', timestamp, 'UTC')
    ),
    upserted_buckets AS (
        INSERT INTO hourly_sentiment_agg (
            symbol, bucket_hour,
            positive_count, neutral_count, negative_count,
            positive_weighted, negative_weighted, neutral_weighted,
            total_weighted, sentiment_index
        )
        SELECT
            symbol,
            bucket_hour,
            positive_count,
            neutral_count,
            negative_count,
            positive_weighted,
            negative_weighted,
            neutral_weighted,
            total_weighted,
            CASE
                WHEN total_weighted > 0
                THEN (positive_weighted - negative_weighted) / total_weighted
                ELSE 0.0
            END
        FROM aggregated_posts
        ORDER BY symbol, bucket_hour
        ON CONFLICT (symbol, bucket_hour) DO UPDATE SET
            positive_count = hourly_sentiment_agg.positive_count
                + EXCLUDED.positive_count,
            neutral_count = hourly_sentiment_agg.neutral_count
                + EXCLUDED.neutral_count,
            negative_count = hourly_sentiment_agg.negative_count
                + EXCLUDED.negative_count,
            positive_weighted = hourly_sentiment_agg.positive_weighted
                + EXCLUDED.positive_weighted,
            negative_weighted = hourly_sentiment_agg.negative_weighted
                + EXCLUDED.negative_weighted,
            neutral_weighted = hourly_sentiment_agg.neutral_weighted
                + EXCLUDED.neutral_weighted,
            total_weighted = hourly_sentiment_agg.total_weighted
                + EXCLUDED.total_weighted,
            sentiment_index = CASE
                WHEN hourly_sentiment_agg.total_weighted
                     + EXCLUDED.total_weighted > 0
                THEN (
                    hourly_sentiment_agg.positive_weighted
                    + EXCLUDED.positive_weighted
                    - hourly_sentiment_agg.negative_weighted
                    - EXCLUDED.negative_weighted
                ) / (
                    hourly_sentiment_agg.total_weighted
                    + EXCLUDED.total_weighted
                )
                ELSE 0.0
            END
        RETURNING 1
    )
    SELECT
        (SELECT COUNT(*) FROM upserted_buckets) AS rolled_up_buckets,
        (SELECT COUNT(*) FROM deleted_posts) AS pruned_posts
"""


class DB:
    def __init__(self, dsn: str = DATABASE_DSN):
        self.dsn = dsn
        self.conn: psycopg.Connection | None = None
        self.async_pool: AsyncConnectionPool | None = None
        self._connect()

    def _connect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except psycopg.Error as exc:
                print(f"Error closing stale database connection: {exc}")
        self.conn = psycopg.connect(self.dsn, autocommit=True)

    async def get_async_pool(self) -> AsyncConnectionPool:
        if self.async_pool is None:
            self.async_pool = AsyncConnectionPool(
                self.dsn, min_size=2, max_size=10, open=False
            )
            await self.async_pool.open()
        return self.async_pool

    async def insert_scored_batch_async(self, posts: list[ScoredPost]) -> int:
        data = [
            (
                p.id,
                p.symbol,
                p.platform,
                p.text.replace("\x00", "") if p.text else "",
                p.timestamp,
                p.sentiment,
                json.dumps(p.scores),
                p.topic_id,
                p.topic_label.replace("\x00", "") if p.topic_label else None,
                p.engagement,
            )
            for p in posts
        ]
        pool = await self.get_async_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(INSERT_POST_SQL, data)
            return cur.rowcount

    def insert_scored_batch(self, posts: list[ScoredPost]) -> int:
        try:
            return self._do_insert_posts_batch(posts)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_insert_posts_batch(posts)

    def _do_insert_posts_batch(self, posts: list[ScoredPost]) -> int:
        data = [
            (
                p.id,
                p.symbol,
                p.platform,
                p.text.replace("\x00", "") if p.text else "",
                p.timestamp,
                p.sentiment,
                json.dumps(p.scores),
                p.topic_id,
                p.topic_label.replace("\x00", "") if p.topic_label else None,
                p.engagement,
            )
            for p in posts
        ]
        assert self.conn is not None
        with self.conn.cursor() as cur:
            # executemany is efficient for small-to-medium batches
            cur.executemany(INSERT_POST_SQL, data)
            # Since ON CONFLICT DO NOTHING is used, we might not get
            # accurate row counts if we want to know how many were NEW.
            # But the primary goal is bulk insertion.
            return cur.rowcount

    def insert_quote(self, quote: StockQuote) -> bool:
        try:
            return self._do_insert_quote(quote)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_insert_quote(quote)

    def _do_insert_quote(self, quote: StockQuote) -> bool:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                INSERT_QUOTE_SQL,
                (
                    quote.symbol,
                    quote.timestamp,
                    quote.price,
                    quote.volume,
                    quote.market_session,
                ),
            )
            return cur.fetchone() is not None

    def upsert_metrics(self, metrics: StockMetrics) -> bool:
        try:
            return self._do_upsert_metrics(metrics)
        except psycopg.OperationalError:
            print("DB connection lost, reconnecting...")
            self._connect()
            return self._do_upsert_metrics(metrics)

    def _do_upsert_metrics(self, metrics: StockMetrics) -> bool:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                UPSERT_METRICS_SQL,
                (
                    metrics.symbol,
                    metrics.pe_ratio,
                    metrics.beta,
                    metrics.avg_return_1y,
                    metrics.inflation_adj_return_1y,
                    metrics.pe_relative_sector,
                    metrics.beta_relative_sector,
                    metrics.return_relative_sector,
                ),
            )
            return cur.fetchone() is not None

    def ensure_us_equity_instruments(self, symbols: list[str]) -> None:
        """Create provider-neutral reference instruments for tracked U.S. stocks."""
        normalized = sorted({symbol.strip().upper() for symbol in symbols if symbol})
        if not normalized:
            return
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO global_instruments (
                    instrument_key, display_name, asset_class, currency,
                    exchange, timezone, provider_aliases, session_metadata
                )
                VALUES (
                    %s, %s, 'us_equity', 'USD', 'NYSE/Nasdaq',
                    'America/New_York', %s::jsonb,
                    '{"open":"09:30","close":"16:00",'
                    '"weekdays":[1,2,3,4,5]}'::jsonb
                )
                ON CONFLICT (instrument_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    provider_aliases = EXCLUDED.provider_aliases,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                [
                    (
                        f"us-stock:{symbol}",
                        symbol,
                        json.dumps({"yahoo": symbol}),
                    )
                    for symbol in normalized
                ],
            )

    def sync_global_instrument_catalog(
        self,
        instruments: list[CatalogInstrument],
    ) -> int:
        """Atomically upsert catalog entries without deleting unlisted rows."""
        if not instruments:
            return 0
        assert self.conn is not None
        values = [
            (
                instrument.instrument_key,
                instrument.display_name,
                instrument.asset_class,
                instrument.currency,
                instrument.exchange,
                instrument.timezone,
                json.dumps(instrument.provider_aliases),
                json.dumps(instrument.session_metadata),
                instrument.quote_convention,
                instrument.is_active,
            )
            for instrument in instruments
        ]
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_INSTRUMENT_SQL, values)
        except psycopg.OperationalError:
            # The upsert is idempotent, so retrying after an unknown commit
            # outcome is safe.
            self._connect()
            assert self.conn is not None
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_INSTRUMENT_SQL, values)
        return len(values)

    def list_active_symbols(self) -> list[str]:
        """Return active tracked symbols directly from the source-of-truth table."""
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol
                FROM tracked_symbols
                WHERE is_active
                ORDER BY symbol
                """
            )
            return [row[0] for row in cur.fetchall()]

    def list_global_instruments(self) -> list[dict]:
        assert self.conn is not None
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT instrument_key, display_name, asset_class, currency,
                       exchange, timezone, provider_aliases, session_metadata,
                       quote_convention
                FROM global_instruments
                WHERE is_active
                ORDER BY asset_class, instrument_key
                """
            )
            return cur.fetchall()

    def upsert_global_bars(
        self,
        bars: list[NormalizedMarketBar],
    ) -> int:
        if not bars:
            return 0
        assert self.conn is not None
        values = [
            (
                bar.instrument_key,
                bar.interval,
                bar.starts_at,
                bar.ends_at,
                bar.session_date,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.volume,
                bar.provider,
            )
            for bar in bars
        ]
        try:
            with self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_BAR_SQL, values)
        except psycopg.OperationalError:
            print("DB connection lost during global bar upsert, reconnecting...")
            self._connect()
            assert self.conn is not None
            with self.conn.cursor() as cur:
                cur.executemany(UPSERT_GLOBAL_BAR_SQL, values)
        return len(values)

    def rollup_and_prune_posts(self, cutoff_ts) -> tuple[int, int]:
        """Atomically aggregate and delete posts older than ``cutoff_ts``.

        The aggregate is derived from the exact rows returned by DELETE. Existing
        buckets are incremented so late-arriving posts are retained. Returns
        ``(rolled_up_buckets, pruned_posts)``.
        """
        try:
            return self._do_rollup_and_prune_posts(cutoff_ts)
        except psycopg.OperationalError:
            # Retrying is safe even when commit status is unknown: if the first
            # statement committed, its source posts no longer exist; otherwise
            # PostgreSQL rolled back both the delete and aggregate upsert.
            print("DB connection lost during post retention, reconnecting...")
            self._connect()
            return self._do_rollup_and_prune_posts(cutoff_ts)

    def _do_rollup_and_prune_posts(self, cutoff_ts) -> tuple[int, int]:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                ROLLUP_AND_PRUNE_POSTS_SQL,
                [POST_RETENTION_ADVISORY_LOCK_KEY, cutoff_ts],
            )
            result = cur.fetchone()
            assert result is not None
            rolled_up_buckets, pruned_posts = result
            return rolled_up_buckets, pruned_posts

    def prune_old_quotes(self, cutoff_ts) -> int:
        """Delete stock quotes older than cutoff_ts. Returns the number of rows deleted."""
        sql = "DELETE FROM stock_quotes WHERE timestamp < %s"
        assert self.conn is not None
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount
        except psycopg.OperationalError:
            print("DB connection lost during quote prune, reconnecting...")
            self._connect()
            assert self.conn is not None
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount

    def prune_global_context(
        self,
        *,
        hourly_cutoff: datetime,
        daily_cutoff: datetime,
        event_cutoff: datetime,
    ) -> tuple[int, int, int]:
        """Apply the 180-day/5-year/1-year context retention policy."""
        assert self.conn is not None
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM global_market_bars
                WHERE interval = '1h' AND ends_at < %s
                """,
                [hourly_cutoff],
            )
            hourly = cur.rowcount
            cur.execute(
                """
                DELETE FROM global_market_bars
                WHERE interval = '1d' AND ends_at < %s
                """,
                [daily_cutoff],
            )
            daily = cur.rowcount
            cur.execute(
                """
                DELETE FROM global_event_signals
                WHERE occurred_at < %s
                """,
                [event_cutoff],
            )
            events = cur.rowcount
        return hourly, daily, events
