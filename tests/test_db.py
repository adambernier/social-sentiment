import pytest
import psycopg
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from uuid import UUID
from shared.schemas import ScoredPost, StockQuote, StockMetrics
from db import (
    DB,
    INSERT_POST_SQL,
    POST_RETENTION_ADVISORY_LOCK_KEY,
    ROLLUP_AND_PRUNE_POSTS_SQL,
    UPSERT_PIPELINE_QUALITY_SQL,
)


def test_insert_post_conflict_scope_matches_source_identity():
    sql = " ".join(INSERT_POST_SQL.lower().split())

    assert "on conflict (platform, id, symbol) do nothing" in sql
    assert "on conflict (id)" not in sql


def _scored_post(post_id: str, platform: str = "bluesky") -> ScoredPost:
    return ScoredPost(
        id=post_id,
        symbol="NVDA",
        platform=platform,
        text="clean text",
        timestamp=datetime(2026, 8, 5, tzinfo=timezone.utc),
        sentiment="positive",
        scores={"positive": 0.9, "neutral": 0.1, "negative": 0.0},
    )


@pytest.fixture
def mock_psycopg_connect():
    with patch("db.psycopg.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        # Cursor mock
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        yield mock_connect, mock_conn, mock_cursor


def test_db_construction_only_opens_connection(mock_psycopg_connect):
    mock_connect, _, mock_cursor = mock_psycopg_connect

    db = DB("mock_dsn")

    assert db.conn is not None
    mock_connect.assert_called_once_with("mock_dsn", autocommit=True)
    mock_cursor.execute.assert_not_called()


def test_insert_scored_batch_records_conflicts_as_quality_facts(
    mock_psycopg_connect,
):
    _, _, mock_cursor = mock_psycopg_connect
    mock_cursor.rowcount = 1
    db = DB("mock_dsn")
    posts = [_scored_post("one"), _scored_post("duplicate")]

    assert db.insert_scored_batch(posts) == 1

    mock_cursor.executemany.assert_called_once()
    mock_cursor.execute.assert_called_once_with(
        UPSERT_PIPELINE_QUALITY_SQL,
        ("bluesky", 1),
    )

def test_db_insert_quote_success(mock_psycopg_connect):
    mock_connect, mock_conn, mock_cursor = mock_psycopg_connect
    
    # Setup mock to return a row ID (success case)
    mock_cursor.fetchone.return_value = (1,)
    
    db = DB("mock_dsn")
    
    quote = StockQuote(
        symbol="AAPL",
        timestamp=datetime.now(timezone.utc),
        price=150.0,
        volume=1000000,
        market_session="regular"
    )
    
    inserted = db.insert_quote(quote)
    
    assert inserted is True
    # Ensure insert query is called
    mock_cursor.execute.assert_called()

def test_db_insert_quote_conflict(mock_psycopg_connect):
    mock_connect, mock_conn, mock_cursor = mock_psycopg_connect
    
    # Setup mock to return None (conflict / DO NOTHING case)
    mock_cursor.fetchone.return_value = None
    
    db = DB("mock_dsn")
    
    quote = StockQuote(
        symbol="AAPL",
        timestamp=datetime.now(timezone.utc),
        price=150.0,
        volume=1000000,
        market_session="regular"
    )
    
    inserted = db.insert_quote(quote)

    assert inserted is False


@pytest.mark.parametrize(
    "database_result",
    [
        pytest.param((3, 12), id="rows-moved"),
        pytest.param((0, 0), id="repeat-run-noop"),
    ],
)
def test_rollup_and_prune_posts_executes_one_atomic_move(
    mock_psycopg_connect,
    database_result,
):
    _, _, mock_cursor = mock_psycopg_connect
    db = DB("mock_dsn")
    mock_cursor.reset_mock()
    mock_cursor.fetchone.return_value = database_result
    cutoff = datetime(2026, 7, 14, 16, tzinfo=timezone.utc)

    result = db.rollup_and_prune_posts(cutoff)

    assert result == database_result
    assert mock_cursor.execute.call_count == 2
    preflight_call, rollup_call = mock_cursor.execute.call_args_list
    assert "ensure_hourly_analytical_partitions" in preflight_call.args[0]
    assert preflight_call.args[1] == [
        POST_RETENTION_ADVISORY_LOCK_KEY,
        cutoff,
        cutoff,
    ]
    assert rollup_call.args[0] == ROLLUP_AND_PRUNE_POSTS_SQL
    params = rollup_call.args[1]
    assert params[0] == POST_RETENTION_ADVISORY_LOCK_KEY
    assert isinstance(params[1], UUID)
    assert params[2:] == [
        cutoff,
        0.01,
        [],
        0.01,
        100,
        0.8,
        [],
        100,
        0.8,
    ]


def test_rollup_and_prune_sql_aggregates_exactly_the_deleted_rows():
    sql = " ".join(ROLLUP_AND_PRUNE_POSTS_SQL.lower().split())

    assert sql.count("delete from posts") == 1
    assert "where p.timestamp < %s" in sql
    assert "where p.timestamp <= %s" not in sql
    assert "using retention_candidates as c" in sql
    assert "p.symbol, p.platform, p.timestamp, p.sentiment, p.engagement" in sql
    assert "from deleted_posts" in sql
    assert "date_trunc('hour', timestamp, 'utc')" in sql

    # Both deletion and aggregation are data-modifying CTEs in one statement;
    # there is no second prune statement that could observe a different snapshot.
    assert "with maintenance_run as materialized" in sql
    assert "legacy_upserts as ( insert into hourly_sentiment_agg" in sql
    assert "fact_upserts as ( insert into hourly_sentiment_facts" in sql
    assert "insert into hourly_sentiment_fact_snapshots" in sql
    assert "insert into raw_post_archive_staging" in sql
    assert ROLLUP_AND_PRUNE_POSTS_SQL.strip().count(";") == 0


def test_rollup_and_prune_sql_adds_late_posts_and_recomputes_sentiment():
    sql = " ".join(ROLLUP_AND_PRUNE_POSTS_SQL.lower().split())

    for column in (
        "positive_count",
        "neutral_count",
        "negative_count",
        "positive_weighted",
        "neutral_weighted",
        "negative_weighted",
        "total_weighted",
    ):
        assert (
            f"{column} = hourly_sentiment_agg.{column} + excluded.{column}"
            in sql
        )

    # The stored index must be derived from combined old/new weights, rather
    # than retaining or overwriting either batch's index.
    sentiment_update = sql.split("sentiment_index = case", maxsplit=1)[1]
    assert "hourly_sentiment_agg.positive_weighted" in sentiment_update
    assert "excluded.positive_weighted" in sentiment_update
    assert "hourly_sentiment_agg.negative_weighted" in sentiment_update
    assert "excluded.negative_weighted" in sentiment_update
    assert "hourly_sentiment_agg.total_weighted" in sentiment_update
    assert "excluded.total_weighted" in sentiment_update


def test_rollup_and_prune_sql_serializes_concurrent_maintenance():
    sql = " ".join(ROLLUP_AND_PRUNE_POSTS_SQL.lower().split())

    assert "pg_advisory_xact_lock(%s)" in sql
    assert "exists (select 1 from maintenance_run)" in sql
    # A stable upsert order further avoids conflicting bucket locks being taken
    # in different orders if maintenance implementations evolve.
    assert "from legacy_aggregates order by symbol, bucket_hour" in sql
    assert "order by a.bucket_hour, a.symbol, a.platform, a.topic_id" in sql


def test_rollup_and_prune_posts_retries_unknown_commit_safely(
    mock_psycopg_connect,
):
    mock_connect, _, mock_cursor = mock_psycopg_connect
    db = DB("mock_dsn")
    mock_cursor.reset_mock()
    mock_cursor.execute.side_effect = [
        psycopg.OperationalError("connection lost"),
        None,
        None,
    ]
    mock_cursor.fetchone.return_value = (1, 4)
    cutoff = datetime(2026, 7, 14, 16, tzinfo=timezone.utc)

    assert db.rollup_and_prune_posts(cutoff) == (1, 4)
    assert mock_cursor.execute.call_count == 3
    assert mock_connect.call_count == 2


def test_rollup_and_prune_posts_propagates_statement_failure(
    mock_psycopg_connect,
):
    _, mock_conn, mock_cursor = mock_psycopg_connect
    db = DB("mock_dsn")
    mock_cursor.reset_mock()
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cursor.execute.side_effect = [
        None,
        psycopg.errors.CheckViolation("aggregate write failed"),
    ]

    with pytest.raises(psycopg.errors.CheckViolation):
        db.rollup_and_prune_posts(
            datetime(2026, 7, 14, 16, tzinfo=timezone.utc)
        )

    # No exception is swallowed and there is no follow-up DELETE. PostgreSQL
    # therefore rolls the data-modifying CTE statement back as one unit.
    assert mock_cursor.execute.call_count == 2
