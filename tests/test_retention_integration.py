import importlib
import math
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from db import (
    POST_RETENTION_ADVISORY_LOCK_KEY,
    QUOTE_RETENTION_ADVISORY_LOCK_KEY,
    ROLLUP_AND_PRUNE_POSTS_SQL,
    ROLLUP_AND_PRUNE_QUOTES_SQL,
)

api_main = importlib.import_module("api-service.main")


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
        storage_dir = Path(__file__).resolve().parent.parent / "storage-service"
        for migration_name in (
            "schema.sql",
            "0003_analytical_facts.sql",
            "0004_fact_topic_labels.sql",
            "0005_provenance_and_quality.sql",
            "0006_analytical_constraints.sql",
        ):
            conn.execute((storage_dir / migration_name).read_text())

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


def _run_retention(
    conn,
    cutoff,
    *,
    archive_platforms=(),
    sample_rate=0.01,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                pg_advisory_xact_lock(%s),
                ensure_hourly_analytical_partitions(
                    COALESCE(
                        (SELECT MIN(timestamp) FROM posts
                         WHERE timestamp < %s),
                        %s
                    ),
                    NOW() + INTERVAL '3 months'
                )
            """,
            [POST_RETENTION_ADVISORY_LOCK_KEY, cutoff, cutoff],
        )
        cur.execute(
            ROLLUP_AND_PRUNE_POSTS_SQL,
            [
                POST_RETENTION_ADVISORY_LOCK_KEY,
                uuid.uuid4(),
                cutoff,
                sample_rate,
                list(archive_platforms),
                sample_rate,
                100,
                0.8,
                list(archive_platforms),
                100,
                0.8,
            ],
        )
        return cur.fetchone()[:2]


def _run_quote_retention(conn, cutoff):
    return conn.execute(
        ROLLUP_AND_PRUNE_QUOTES_SQL,
        [QUOTE_RETENTION_ADVISORY_LOCK_KEY, cutoff],
    ).fetchone()


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
                    id, symbol, platform, text, timestamp, sentiment,
                    scores, engagement
                )
                VALUES (%s, %s, 'bluesky', 'clean text', %s, %s, %s::jsonb, %s)
                """,
                [
                    (
                        "late-negative",
                        "NVDA",
                        bucket_hour + timedelta(minutes=45),
                        "negative",
                        '{"positive":0.1,"neutral":0.1,"negative":0.8}',
                        3,
                    ),
                    (
                        "at-cutoff",
                        "NVDA",
                        cutoff,
                        "positive",
                        '{"positive":0.8,"neutral":0.1,"negative":0.1}',
                        1,
                    ),
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
        canonical = conn.execute(
            """
            SELECT post_count, negative_count, negative_probability_sum,
                   signal_sum, weight_sum
            FROM hourly_sentiment_facts
            WHERE symbol = 'NVDA' AND bucket_hour = %s
            """,
            [bucket_hour],
        ).fetchone()
        assert canonical == pytest.approx(
            (1, 1, 0.8, -0.7, late_negative_weight)
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM hourly_sentiment_fact_snapshots"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT model_kind, model_hash FROM model_artifacts ORDER BY model_kind"
        ).fetchall() == [("sentiment", "legacy"), ("topic", "legacy")]

        fact_query, fact_params, is_filtered = (
            api_main._sentiment_aggregate_query(
                "NVDA",
                bucket_hour,
                "bluesky",
                "Unassigned",
            )
        )
        assert is_filtered is True
        assert conn.execute(fact_query, fact_params).fetchone()[1:4] == (0, 0, 1)
        coverage_query, coverage_params = api_main._sentiment_coverage_query(
            "NVDA",
            "bluesky",
            "Unassigned",
        )
        assert conn.execute(
            coverage_query,
            coverage_params,
        ).fetchone() == (bucket_hour,)


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
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                engagement
            )
            VALUES (
                'must-survive', 'NVDA', 'bluesky', 'clean text', %s,
                'positive', '{"positive":1,"neutral":0,"negative":0}', 1
            )
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
                    id, symbol, platform, text, timestamp, sentiment,
                    scores, engagement
                )
                VALUES (
                    %s, 'NVDA', 'bluesky', 'clean text', %s, 'positive',
                    '{"positive":1,"neutral":0,"negative":0}', 1
                )
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
        assert conn.execute(
            "SELECT post_count FROM hourly_sentiment_facts"
        ).fetchone() == (post_count,)


def test_archive_sampling_is_opt_in_and_cohorts_stay_separate(
    retention_database,
):
    cutoff = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
    with _connect_to_retention_database(retention_database) as conn:
        conn.execute(
            """
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                engagement
            )
            VALUES (
                'archive-me', 'NVDA', 'bluesky', 'cleaned archive text', %s,
                'positive', '{"positive":0.95,"neutral":0.04,"negative":0.01}',
                150
            )
            """,
            [cutoff - timedelta(hours=1)],
        )

        assert _run_retention(
            conn,
            cutoff,
            archive_platforms=("bluesky",),
            sample_rate=1.0,
        ) == (1, 1)

        samples = conn.execute(
            """
            SELECT cohort, inclusion_probability, selection_reasons,
                   sampling_policy_version, stratum
            FROM raw_post_archive_staging
            ORDER BY cohort
            """
        ).fetchall()
        assert [sample[0] for sample in samples] == ["challenge", "probability"]
        assert samples[0][1] is None
        assert set(samples[0][2]) == {"high_engagement", "extreme_sentiment"}
        assert samples[1][1] == 1.0
        assert {sample[3] for sample in samples} == {"sample-v1"}
        assert all(sample[4].startswith("bluesky:") for sample in samples)


def test_quote_retention_preserves_ohlc_and_updates_for_late_quotes(
    retention_database,
):
    bucket_hour = datetime(2026, 7, 14, 14, tzinfo=timezone.utc)
    cutoff = bucket_hour + timedelta(hours=1)
    with _connect_to_retention_database(retention_database) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO stock_quotes (
                    symbol, timestamp, price, volume, market_session
                )
                VALUES ('NVDA', %s, %s, %s, %s)
                """,
                [
                    (bucket_hour + timedelta(minutes=5), 100.0, 10, "pre"),
                    (bucket_hour + timedelta(minutes=30), 105.0, 20, "regular"),
                    (cutoff, 110.0, 30, "regular"),
                ],
            )

        assert _run_quote_retention(conn, cutoff) == (1, 1, 2)
        assert conn.execute(
            """
            SELECT open_price, high_price, low_price, close_price,
                   last_volume, observation_count, had_regular_session,
                   providers
            FROM market_hourly_facts
            """
        ).fetchone() == (
            100.0,
            105.0,
            100.0,
            105.0,
            20,
            2,
            True,
            ["legacy"],
        )
        assert conn.execute(
            "SELECT timestamp FROM stock_quotes"
        ).fetchone() == (cutoff,)

        conn.execute(
            """
            INSERT INTO stock_quotes (
                symbol, timestamp, price, volume, market_session
            )
            VALUES ('NVDA', %s, 103.0, 25, 'after')
            """,
            [bucket_hour + timedelta(minutes=45)],
        )
        assert _run_quote_retention(conn, cutoff) == (1, 1, 1)
        assert conn.execute(
            """
            SELECT open_price, high_price, low_price, close_price,
                   last_volume, observation_count, had_regular_session,
                   providers
            FROM market_hourly_facts
            """
        ).fetchone() == (
            100.0,
            105.0,
            100.0,
            103.0,
            25,
            3,
            True,
            ["legacy"],
        )

        assert _run_quote_retention(conn, cutoff) == (0, 0, 0)

        hourly_rows = conn.execute(
            api_main.HOURLY_MARKET_QUERY,
            [
                ["NVDA"],
                bucket_hour - timedelta(hours=1),
                ["NVDA"],
                bucket_hour - timedelta(hours=1),
            ],
        ).fetchall()
        assert [row[1] for row in hourly_rows] == [bucket_hour, cutoff]
