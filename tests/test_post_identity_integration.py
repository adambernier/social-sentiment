import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from db import INSERT_POST_SQL


TEST_DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")
SCHEMA_SQL = (
    Path(__file__).resolve().parents[1] / "storage-service" / "schema.sql"
).read_text()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_DSN,
    reason="TEST_DATABASE_DSN is required for PostgreSQL identity tests",
)


def _set_search_path(conn, schema_name):
    conn.execute(
        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
    )


@pytest.fixture
def legacy_posts_database():
    schema_name = f"post_identity_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        _set_search_path(conn, schema_name)
        conn.execute("""
            CREATE TABLE posts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL DEFAULT 'UNKNOWN',
                platform TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                sentiment TEXT NOT NULL,
                scores JSONB NOT NULL,
                topic_id INTEGER,
                topic_label TEXT,
                scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                engagement INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores
            )
            VALUES (
                'shared-id', 'AAPL', 'reddit', 'legacy row',
                '2026-07-21T12:00:00Z', 'neutral', '{}'::jsonb
            )
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


def _post_values(symbol, platform, text):
    return (
        "shared-id",
        symbol,
        platform,
        text,
        "2026-07-21T12:01:00Z",
        "positive",
        "{}",
        None,
        None,
        1,
    )


def test_schema_migrates_global_id_primary_key_and_scopes_deduplication(
    legacy_posts_database,
):
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        _set_search_path(conn, legacy_posts_database)

        conn.execute(SCHEMA_SQL)
        conn.execute(SCHEMA_SQL)  # A retried migration must remain idempotent.

        primary_key_columns = conn.execute(
            """
            SELECT array_agg(attribute_info.attname ORDER BY key_info.ordinality)
            FROM pg_constraint AS constraint_info
            CROSS JOIN LATERAL unnest(constraint_info.conkey)
                WITH ORDINALITY AS key_info(attnum, ordinality)
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = constraint_info.conrelid
             AND attribute_info.attnum = key_info.attnum
            WHERE constraint_info.conrelid = 'posts'::regclass
              AND constraint_info.contype = 'p'
            """
        ).fetchone()[0]
        assert primary_key_columns == ["post_pk"]

        legacy_key = conn.execute(
            "SELECT post_pk FROM posts WHERE text = 'legacy row'"
        ).fetchone()[0]
        assert legacy_key is not None

        with conn.cursor() as cur:
            cur.execute(
                INSERT_POST_SQL,
                _post_values("MSFT", "reddit", "second symbol"),
            )
            assert cur.fetchone() == ("shared-id",)

            cur.execute(
                INSERT_POST_SQL,
                _post_values("AAPL", "reddit", "exact redelivery"),
            )
            assert cur.fetchone() is None

            cur.execute(
                INSERT_POST_SQL,
                _post_values("AAPL", "bluesky", "other platform"),
            )
            assert cur.fetchone() == ("shared-id",)

        rows = conn.execute("""
            SELECT post_pk, platform, id, symbol
            FROM posts
            ORDER BY platform, symbol
        """).fetchall()
        assert len(rows) == 3
        assert len({row[0] for row in rows}) == 3
        assert {(row[1], row[2], row[3]) for row in rows} == {
            ("reddit", "shared-id", "AAPL"),
            ("reddit", "shared-id", "MSFT"),
            ("bluesky", "shared-id", "AAPL"),
        }
