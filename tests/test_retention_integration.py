import math
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import sql

from db import POST_RETENTION_ADVISORY_LOCK_KEY, ROLLUP_AND_PRUNE_POSTS_SQL


TEST_DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_DSN,
    reason="TEST_DATABASE_DSN is required for PostgreSQL retention tests",
)


def _set_search_path(conn, schema_name):
    conn.execute(
        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
    )


@pytest.fixture
def retention_database():
    schema_name = f"retention_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )
        _set_search_path(conn, schema_name)
        conn.execute("""
            CREATE TABLE posts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                sentiment TEXT NOT NULL,
                engagement INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE hourly_sentiment_agg (
                symbol TEXT NOT NULL,
                bucket_hour TIMESTAMPTZ NOT NULL,
                positive_count INTEGER NOT NULL DEFAULT 0,
                neutral_count INTEGER NOT NULL DEFAULT 0,
                negative_count INTEGER NOT NULL DEFAULT 0,
                positive_weighted FLOAT NOT NULL DEFAULT 0,
                negative_weighted FLOAT NOT NULL DEFAULT 0,
                neutral_weighted FLOAT NOT NULL DEFAULT 0,
                total_weighted FLOAT NOT NULL DEFAULT 0,
                sentiment_index FLOAT NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, bucket_hour)
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


def _connect_to_retention_database(schema_name):
    conn = psycopg.connect(TEST_DATABASE_DSN, autocommit=True)
    _set_search_path(conn, schema_name)
    return conn


def _run_retention(conn, cutoff):
    with conn.cursor() as cur:
        cur.execute(
            ROLLUP_AND_PRUNE_POSTS_SQL,
            [POST_RETENTION_ADVISORY_LOCK_KEY, cutoff],
        )
        return cur.fetchone()


def test_retention_adds_late_posts_preserves_boundary_and_repeats_safely(
    retention_database,
):
    bucket_hour = datetime(2026, 7, 14, 16, tzinfo=timezone.utc)
    cutoff = bucket_hour + timedelta(hours=2)
    existing_positive_weight = math.log(2.0)
    late_negative_weight = math.log(4.0)

    with _connect_to_retention_database(retention_database) as conn:
        conn.execute(
            """
            INSERT INTO hourly_sentiment_agg (
                symbol, bucket_hour, positive_count, positive_weighted,
                total_weighted, sentiment_index
            )
            VALUES (%s, %s, 1, %s, %s, 1.0)
            """,
            [
                "NVDA",
                bucket_hour,
                existing_positive_weight,
                existing_positive_weight,
            ],
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO posts (
                    id, symbol, timestamp, sentiment, engagement
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        "late-negative",
                        "NVDA",
                        bucket_hour + timedelta(minutes=45),
                        "negative",
                        3,
                    ),
                    ("at-cutoff", "NVDA", cutoff, "positive", 1),
                ],
            )

        assert _run_retention(conn, cutoff) == (1, 1)

        aggregate = conn.execute(
            """
            SELECT positive_count, neutral_count, negative_count,
                   positive_weighted, negative_weighted, total_weighted,
                   sentiment_index
            FROM hourly_sentiment_agg
            WHERE symbol = 'NVDA' AND bucket_hour = %s
            """,
            [bucket_hour],
        ).fetchone()
        expected_total = existing_positive_weight + late_negative_weight
        expected_index = (
            existing_positive_weight - late_negative_weight
        ) / expected_total
        assert aggregate == pytest.approx(
            (
                1,
                0,
                1,
                existing_positive_weight,
                late_negative_weight,
                expected_total,
                expected_index,
            )
        )

        # The strict cutoff leaves the boundary row in the hot tier.
        assert conn.execute(
            "SELECT id FROM posts ORDER BY id"
        ).fetchall() == [("at-cutoff",)]

        # A repeat run neither changes the aggregate nor deletes the boundary.
        assert _run_retention(conn, cutoff) == (0, 0)
        assert conn.execute(
            "SELECT positive_count, negative_count FROM hourly_sentiment_agg"
        ).fetchone() == (1, 1)


def test_retention_rolls_back_delete_when_aggregate_write_fails(
    retention_database,
):
    cutoff = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
    with _connect_to_retention_database(retention_database) as conn:
        conn.execute("""
            ALTER TABLE hourly_sentiment_agg
            ADD CONSTRAINT reject_positive CHECK (positive_count = 0)
        """)
        conn.execute(
            """
            INSERT INTO posts (id, symbol, timestamp, sentiment, engagement)
            VALUES ('must-survive', 'NVDA', %s, 'positive', 1)
            """,
            [cutoff - timedelta(hours=1)],
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            _run_retention(conn, cutoff)

        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM hourly_sentiment_agg"
        ).fetchone() == (0,)


def test_concurrent_retention_moves_each_post_once(retention_database):
    bucket_hour = datetime(2026, 7, 14, 16, tzinfo=timezone.utc)
    cutoff = bucket_hour + timedelta(hours=2)
    post_count = 40
    with _connect_to_retention_database(retention_database) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO posts (
                    id, symbol, timestamp, sentiment, engagement
                )
                VALUES (%s, 'NVDA', %s, 'positive', 1)
                """,
                [
                    (f"post-{index}", bucket_hour + timedelta(minutes=index))
                    for index in range(post_count)
                ],
            )

    start = threading.Barrier(2)

    def run_worker():
        with _connect_to_retention_database(retention_database) as conn:
            start.wait(timeout=5)
            return _run_retention(conn, cutoff)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_worker(), range(2)))

    assert sum(result[0] for result in results) == 1
    assert sum(result[1] for result in results) == post_count
    with _connect_to_retention_database(retention_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone() == (0,)
        assert conn.execute(
            "SELECT positive_count FROM hourly_sentiment_agg"
        ).fetchone() == (post_count,)
