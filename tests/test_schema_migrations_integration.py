import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql


TEST_DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")
SCHEMA_SQL = (
    Path(__file__).resolve().parents[1] / "storage-service" / "schema.sql"
).read_text()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_DSN,
    reason="TEST_DATABASE_DSN is required for PostgreSQL migration tests",
)


def _set_search_path(conn, schema_name):
    conn.execute(
        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
    )


@pytest.fixture
def legacy_quotes_database():
    schema_name = f"quote_migration_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        _set_search_path(conn, schema_name)
        conn.execute("""
            CREATE TABLE stock_quotes (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                price FLOAT NOT NULL,
                volume BIGINT NOT NULL,
                market_session TEXT NOT NULL DEFAULT 'closed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            INSERT INTO stock_quotes (
                symbol, timestamp, price, volume, market_session
            )
            VALUES
                ('NVDA', '2026-07-21T12:00:00Z', 170.0, 100, 'regular'),
                ('NVDA', '2026-07-21T12:00:00Z', 171.0, 200, 'regular')
        """)

    try:
        yield schema_name
    finally:
        with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def test_quote_cleanup_runs_only_during_uniqueness_migration(
    legacy_quotes_database,
):
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        _set_search_path(conn, legacy_quotes_database)

        conn.execute(SCHEMA_SQL)

        quotes = conn.execute("""
            SELECT id, price
            FROM stock_quotes
            ORDER BY id
        """).fetchall()
        assert quotes == [(1, 170.0)]

        constraint_columns = conn.execute("""
            SELECT array_agg(
                attribute_info.attname::TEXT ORDER BY key_info.ordinality
            )
            FROM pg_constraint AS constraint_info
            CROSS JOIN LATERAL unnest(constraint_info.conkey)
                WITH ORDINALITY AS key_info(attnum, ordinality)
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = constraint_info.conrelid
             AND attribute_info.attnum = key_info.attnum
            WHERE constraint_info.conrelid = 'stock_quotes'::regclass
              AND constraint_info.contype = 'u'
            GROUP BY constraint_info.conname
        """).fetchall()
        assert ["symbol", "timestamp"] in [row[0] for row in constraint_columns]

        # A statement trigger catches even a DELETE that matches zero rows. Once
        # uniqueness exists, schema reapplication must skip DELETE altogether.
        conn.execute("""
            CREATE FUNCTION reject_repeated_quote_cleanup()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'stock quote cleanup ran more than once';
            END;
            $$ LANGUAGE plpgsql
        """)
        conn.execute("""
            CREATE TRIGGER reject_repeated_quote_cleanup
            BEFORE DELETE ON stock_quotes
            FOR EACH STATEMENT
            EXECUTE FUNCTION reject_repeated_quote_cleanup()
        """)

        conn.execute(SCHEMA_SQL)
        assert conn.execute(
            "SELECT COUNT(*) FROM stock_quotes"
        ).fetchone() == (1,)
