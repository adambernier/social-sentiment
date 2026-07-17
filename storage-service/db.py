import json
import random
import sys
import time
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import ScoredPost, StockQuote, StockMetrics
from shared.config import DATABASE_DSN

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Schema is applied on startup and can include DDL (e.g. ADD CONSTRAINT) that takes
# an exclusive lock on a busy table. Concurrent readers/writers can deadlock it (or
# it can block), and an unhandled error there crashes the whole consumer — stranding
# the pipeline. So apply with a short lock_timeout (fail fast) and retry transient
# lock errors instead of dying.
SCHEMA_APPLY_RETRIES = 5
SCHEMA_LOCK_TIMEOUT_MS = 5000
SCHEMA_RETRY_BASE_DELAY = 1.0
# Several services (storage-service, market-producer, ...) construct DB() on boot
# and each applies the schema, so on `compose up` multiple processes run the DDL
# at once — and concurrent DDL (the DELETE self-join + ADD CONSTRAINT below) is
# exactly what deadlocks. A session-level advisory lock on this fixed key
# serializes the starters so only one runs the DDL at a time; the schema is
# idempotent, so the others re-run it harmlessly. Released automatically if the
# session drops. The key is arbitrary but must match across all callers.
SCHEMA_ADVISORY_LOCK_KEY = 4815162342

INSERT_POST_SQL = """
    INSERT INTO posts (id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, engagement)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO NOTHING
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


class DB:
    def __init__(self, dsn: str = DATABASE_DSN):
        self.dsn = dsn
        self.conn: psycopg.Connection | None = None
        self.async_pool: AsyncConnectionPool | None = None
        self._connect()
        self._apply_schema()

    def _connect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = psycopg.connect(self.dsn, autocommit=True)

    async def get_async_pool(self) -> AsyncConnectionPool:
        if self.async_pool is None:
            self.async_pool = AsyncConnectionPool(
                self.dsn,
                min_size=2,
                max_size=10,
                open=False
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
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(INSERT_POST_SQL, data)
                return cur.rowcount

    def _apply_schema(self) -> None:
        sql = SCHEMA_FILE.read_text()
        last_err: Exception | None = None
        for attempt in range(1, SCHEMA_APPLY_RETRIES + 1):
            try:
                with self.conn.cursor() as cur:
                    # Serialize concurrent starters so no two processes run the
                    # DDL at once (the actual deadlock trigger). Acquired before
                    # lock_timeout is set, so it waits for our turn rather than
                    # failing fast; the holder finishes quickly when uncontended.
                    cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
                    try:
                        # Within our turn, still fail fast rather than block
                        # forever if *live* pipeline traffic holds a table lock
                        # the migration needs.
                        cur.execute(f"SET lock_timeout = '{SCHEMA_LOCK_TIMEOUT_MS}ms'")
                        cur.execute(sql)
                    finally:
                        # Best-effort: session close on reconnect/exit releases it
                        # anyway, so don't let an unlock error mask a real failure.
                        try:
                            cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
                        except Exception:
                            pass
                return
            except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable) as e:
                last_err = e
                if attempt >= SCHEMA_APPLY_RETRIES:
                    break
                delay = SCHEMA_RETRY_BASE_DELAY * attempt + random.uniform(0, SCHEMA_RETRY_BASE_DELAY)
                print(
                    f"Schema apply blocked by lock contention ({type(e).__name__}); "
                    f"attempt {attempt}/{SCHEMA_APPLY_RETRIES}, retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                # The aborted statement leaves the session usable under autocommit,
                # but reconnect for a clean slate (and reset the timed-out lock wait).
                self._connect()
        print(f"Schema apply failed after {SCHEMA_APPLY_RETRIES} attempts: {last_err}")
        raise last_err

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

    def rollup_to_aggregates(self, cutoff_ts) -> int:
        """Roll up posts older than cutoff_ts into hourly_sentiment_agg.
        
        Uses INSERT ... ON CONFLICT to be idempotent — safe to run repeatedly.
        Returns the number of rows upserted.
        """
        sql = """
            INSERT INTO hourly_sentiment_agg (
                symbol, bucket_hour,
                positive_count, neutral_count, negative_count,
                positive_weighted, negative_weighted, neutral_weighted,
                total_weighted, sentiment_index
            )
            SELECT
                symbol,
                date_trunc('hour', timestamp) AS bucket_hour,
                COUNT(*) FILTER (WHERE sentiment = 'positive') AS positive_count,
                COUNT(*) FILTER (WHERE sentiment = 'neutral') AS neutral_count,
                COUNT(*) FILTER (WHERE sentiment = 'negative') AS negative_count,
                COALESCE(SUM(LN(engagement + 1.0)) FILTER (WHERE sentiment = 'positive'), 0) AS positive_weighted,
                COALESCE(SUM(LN(engagement + 1.0)) FILTER (WHERE sentiment = 'negative'), 0) AS negative_weighted,
                COALESCE(SUM(LN(engagement + 1.0)) FILTER (WHERE sentiment = 'neutral'), 0) AS neutral_weighted,
                COALESCE(SUM(LN(engagement + 1.0)), 0) AS total_weighted,
                CASE
                    WHEN COALESCE(SUM(LN(engagement + 1.0)), 0) > 0
                    THEN (
                        COALESCE(SUM(LN(engagement + 1.0)) FILTER (WHERE sentiment = 'positive'), 0)
                        - COALESCE(SUM(LN(engagement + 1.0)) FILTER (WHERE sentiment = 'negative'), 0)
                    )::float / SUM(LN(engagement + 1.0))
                    ELSE 0.0
                END AS sentiment_index
            FROM posts
            WHERE timestamp < %s
            GROUP BY symbol, date_trunc('hour', timestamp)
            ON CONFLICT (symbol, bucket_hour) DO UPDATE SET
                positive_count = EXCLUDED.positive_count,
                neutral_count = EXCLUDED.neutral_count,
                negative_count = EXCLUDED.negative_count,
                positive_weighted = EXCLUDED.positive_weighted,
                negative_weighted = EXCLUDED.negative_weighted,
                neutral_weighted = EXCLUDED.neutral_weighted,
                total_weighted = EXCLUDED.total_weighted,
                sentiment_index = EXCLUDED.sentiment_index
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount
        except psycopg.OperationalError:
            print("DB connection lost during rollup, reconnecting...")
            self._connect()
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount

    def prune_old_posts(self, cutoff_ts) -> int:
        """Delete posts older than cutoff_ts. Returns the number of rows deleted."""
        sql = "DELETE FROM posts WHERE timestamp < %s"
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount
        except psycopg.OperationalError:
            print("DB connection lost during prune, reconnecting...")
            self._connect()
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount

    def prune_old_quotes(self, cutoff_ts) -> int:
        """Delete stock quotes older than cutoff_ts. Returns the number of rows deleted."""
        sql = "DELETE FROM stock_quotes WHERE timestamp < %s"
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount
        except psycopg.OperationalError:
            print("DB connection lost during quote prune, reconnecting...")
            self._connect()
            with self.conn.cursor() as cur:
                cur.execute(sql, [cutoff_ts])
                return cur.rowcount
