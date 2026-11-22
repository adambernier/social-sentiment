import math
import os
import re
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
        migrations_dir = Path(__file__).resolve().parent.parent / "schema-migrate"
        for migration_name in (
            "schema.sql",
            "0003_analytical_facts.sql",
            "0004_fact_topic_labels.sql",
            "0005_provenance_and_quality.sql",
            "0006_analytical_constraints.sql",
            "0007_stock_metrics_constraints.sql",
            "0008_index_optimization.sql",
        ):
            conn.execute((migrations_dir / migration_name).read_text())

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


def test_boundary_simplex_posts_roll_up_cleanly_and_stock_metrics_reject_non_finite(
    retention_database,
):
    bucket_hour = datetime(2026, 7, 15, 14, tzinfo=timezone.utc)
    cutoff = bucket_hour + timedelta(hours=1)
    with _connect_to_retention_database(retention_database) as conn:
        # Posts at the tightened 5e-7 ingestion boundary (sum = 1.0000004) must
        # satisfy 0006's 1e-6 per-post probability CHECK when rolled up.
        conn.execute(
            """
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                topic_id, topic_label, engagement
            )
            VALUES (
                'boundary-1', 'NVDA', 'bluesky', 'Boundary simplex post',
                %s, 'positive',
                '{"positive": 0.7000004, "neutral": 0.2, "negative": 0.1}'::jsonb,
                0, 'Earnings & Guidance', 5
            )
            """,
            [bucket_hour + timedelta(minutes=10)],
        )
        assert _run_retention(conn, cutoff) == (1, 1)
        assert conn.execute(
            "SELECT post_count FROM hourly_sentiment_facts WHERE symbol = 'NVDA'"
        ).fetchone() == (1,)

        # Migration 0007 enforces finite sector-relative scores on stock_metrics.
        conn.execute(
            """
            INSERT INTO stock_metrics (
                symbol, pe_relative_sector, beta_relative_sector, return_relative_sector
            )
            VALUES ('NVDA', 0.25, -0.1, 0.4)
            """
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                UPDATE stock_metrics
                SET pe_relative_sector = 'NaN'::double precision
                WHERE symbol = 'NVDA'
                """
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                UPDATE stock_metrics
                SET return_relative_sector = 'Infinity'::double precision
                WHERE symbol = 'NVDA'
                """
            )


def _load_api_sql_constant(const_name: str) -> str:
    api_main = (
        Path(__file__).resolve().parent.parent
        / "crates"
        / "api-service"
        / "src"
        / "main.rs"
    ).read_text()
    match = re.search(
        rf'const {re.escape(const_name)}: &str = r#"(.*?)"#;',
        api_main,
        flags=re.DOTALL,
    )
    assert match is not None, f"Could not locate {const_name} in api-service/src/main.rs"
    return re.sub(r"\$(\d+)", r"%(p\1)s", match.group(1))


def test_api_sql_cold_hot_tier_unions_and_hot_hourly_aggregation(retention_database):
    now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
    cold_hour = (now - timedelta(days=20)).replace(minute=0)
    hot_hour = (now - timedelta(hours=2)).replace(minute=0)
    retention_cutoff = (now - timedelta(days=14)).replace(minute=0)

    leaderboard_sql = _load_api_sql_constant("LEADERBOARD_SQL")
    sentiment_stats_sql = _load_api_sql_constant("SENTIMENT_STATS_SQL")
    topic_stats_sql = _load_api_sql_constant("TOPIC_STATS_SQL")
    hot_posts_hourly_sql = _load_api_sql_constant("HOT_POSTS_HOURLY_SQL")

    with _connect_to_retention_database(retention_database) as conn:
        conn.execute(
            "INSERT INTO tracked_symbols (symbol, is_active) VALUES ('NVDA', TRUE)"
        )

        # Insert 8 distinct cold-tier hours (~20d ago) plus 1 unassigned-topic post,
        # then roll them into hourly_sentiment_facts and prune from posts.
        for offset in range(8):
            ts = cold_hour + timedelta(hours=offset, minutes=10)
            conn.execute(
                """
                INSERT INTO posts (
                    id, symbol, platform, text, timestamp, sentiment, scores,
                    topic_id, topic_label, engagement
                )
                VALUES (
                    %s, 'NVDA', 'bluesky', 'Cold tier baseline post',
                    %s, 'positive',
                    '{"positive": 0.8, "neutral": 0.1, "negative": 0.1}'::jsonb,
                    %s, %s, 4
                )
                """,
                [
                    f"cold-{offset}",
                    ts,
                    -2 if offset == 0 else 0,
                    None if offset == 0 else "Earnings & Guidance",
                ],
            )
        assert _run_retention(conn, retention_cutoff) == (8, 8)
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone() == (0,)

        # Insert 2 hot-tier posts in the same UTC hour (one with explicit scores,
        # one falling back to sentiment label when scores lacks probabilities).
        conn.execute(
            """
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                topic_id, topic_label, engagement
            )
            VALUES
                (
                    'hot-1', 'NVDA', 'bluesky', 'Hot positive',
                    %s, 'positive',
                    '{"positive": 0.9, "neutral": 0.05, "negative": 0.05}'::jsonb,
                    0, 'Earnings & Guidance', 9
                ),
                (
                    'hot-2', 'NVDA', 'finnhub', 'Hot negative fallback',
                    %s, 'negative',
                    '{}'::jsonb,
                    -2, NULL, 3
                )
            """,
            [hot_hour + timedelta(minutes=5), hot_hour + timedelta(minutes=20)],
        )

        # 1. Leaderboard 28d baseline must retain the 8 pruned cold-tier hours
        #    (and not double-count hourly_sentiment_agg).
        lb_rows = conn.execute(leaderboard_sql).fetchall()
        assert len(lb_rows) == 1
        symbol, post_4h, pos_4h, neg_4h, _idx_4h, _buzz_z, mean_hr, samples = lb_rows[0]
        assert symbol == "NVDA"
        assert (post_4h, pos_4h, neg_4h) == (2, 1, 1)
        assert samples == 8
        assert mean_hr == pytest.approx(1.0)

        # 2. Dashboard / stats sentiment & topic queries must union cold + hot tiers
        #    and merge cold 'Unassigned' with hot NULL topic_label.
        window_cutoff = now - timedelta(days=30)
        sentiment_rows = dict(
            conn.execute(
                sentiment_stats_sql,
                {"p1": window_cutoff, "p2": "NVDA", "p3": None},
            ).fetchall()
        )
        assert sentiment_rows == {"positive": 9, "negative": 1}

        filtered_sentiment = dict(
            conn.execute(
                sentiment_stats_sql,
                {"p1": window_cutoff, "p2": "NVDA", "p3": "bluesky"},
            ).fetchall()
        )
        assert filtered_sentiment == {"positive": 9}

        topic_rows = dict(
            conn.execute(
                topic_stats_sql,
                {"p1": window_cutoff, "p2": "NVDA", "p3": None},
            ).fetchall()
        )
        assert topic_rows == {"Earnings & Guidance": 8, None: 2}

        # 3. Correlation hot-tier SQL aggregation must match per-post math exactly.
        hot_rows = conn.execute(
            hot_posts_hourly_sql,
            {"p1": "NVDA", "p2": window_cutoff, "p3": None, "p4": None},
        ).fetchall()
        assert len(hot_rows) == 1
        (
            b_hour,
            pos_cnt,
            neu_cnt,
            neg_cnt,
            total_w,
            weighted_sig,
            sig_sum,
            sig_sq_sum,
        ) = hot_rows[0]
        assert b_hour == hot_hour
        assert (pos_cnt, neu_cnt, neg_cnt) == (1, 0, 1)
        w1, s1 = math.log1p(9), 0.9 - 0.05
        w2, s2 = math.log1p(3), -1.0
        assert total_w == pytest.approx(w1 + w2)
        assert weighted_sig == pytest.approx(w1 * s1 + w2 * s2)
        assert sig_sum == pytest.approx(s1 + s2)
        assert sig_sq_sum == pytest.approx(s1 * s1 + s2 * s2)


def test_storage_bulk_unnest_insert_handles_duplicates_and_null_topics(
    retention_database,
):
    storage_db = (
        Path(__file__).resolve().parent.parent
        / "crates"
        / "storage-service"
        / "src"
        / "db.rs"
    ).read_text()
    match = re.search(
        r'pub const INSERT_POSTS_BULK_SQL: &str = r#"(.*?)"#;',
        storage_db,
        flags=re.DOTALL,
    )
    assert match is not None
    bulk_sql = re.sub(r"\$(\d+)", r"%(p\1)s", match.group(1))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 3 rows in one UNNEST batch where row 2 duplicates row 1's (platform, id, symbol)
    params = {
        "p1": ["bulk-1", "bulk-1", "bulk-2"],
        "p2": ["NVDA", "NVDA", "NVDA"],
        "p3": ["bluesky", "bluesky", "finnhub"],
        "p4": ["First post", "Duplicate in batch", "Second post"],
        "p5": [now, now, now + timedelta(seconds=10)],
        "p6": ["positive", "positive", "neutral"],
        "p7": [
            '{"positive": 0.8, "neutral": 0.1, "negative": 0.1}',
            '{"positive": 0.8, "neutral": 0.1, "negative": 0.1}',
            '{"positive": 0.1, "neutral": 0.8, "negative": 0.1}',
        ],
        "p8": [0, 0, -2],
        "p9": ["Earnings & Guidance", "Earnings & Guidance", None],
        "p10": [5, 5, 1],
        "p11": [now, now, now],
        "p12": [now, now, now],
        "p13": [now, now, now],
        "p14": [now, now, now],
        "p15": [now, now, now],
        "p16": [1, 1, 1],
        "p17": ["abc123", "abc123", "abc123"],
        "p18": ["zero-shot-onnx-v1"] * 3,
        "p19": ["topic-hash"] * 3,
        "p20": ["fintwitbert-onnx-v1"] * 3,
        "p21": ["sent-hash"] * 3,
    }

    with _connect_to_retention_database(retention_database) as conn:
        cur = conn.execute(bulk_sql, params)
        assert cur.rowcount == 2
        rows = conn.execute(
            "SELECT id, topic_label FROM posts ORDER BY id"
        ).fetchall()
        assert rows == [("bulk-1", "Earnings & Guidance"), ("bulk-2", None)]

        # Migration 0008 drops redundant prefix indexes and adds platform+timestamp.
        index_names = {
            row[0]
            for row in conn.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                """
            ).fetchall()
        }
        assert "posts_symbol_idx" not in index_names
        assert "stock_quotes_symbol_idx" not in index_names
        assert "posts_platform_timestamp_idx" in index_names
