import json
import sys
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.schemas import ScoredPost, StockQuote, StockMetrics
from shared.config import DATABASE_DSN

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

INSERT_POST_SQL = """
    INSERT INTO posts (id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (id) DO NOTHING
    RETURNING id
"""

INSERT_QUOTE_SQL = """
    INSERT INTO stock_quotes (symbol, timestamp, price, volume, market_session)
    VALUES (%s, %s, %s, %s, %s)
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
                p.text,
                p.timestamp,
                p.sentiment,
                json.dumps(p.scores),
                p.topic_id,
                p.topic_label,
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
        with self.conn.cursor() as cur:
            cur.execute(sql)

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
                p.text,
                p.timestamp,
                p.sentiment,
                json.dumps(p.scores),
                p.topic_id,
                p.topic_label,
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
