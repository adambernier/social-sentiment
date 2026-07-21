import pytest
import psycopg
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from shared.schemas import StockQuote, StockMetrics
from db import (
    DB,
    POST_RETENTION_ADVISORY_LOCK_KEY,
    ROLLUP_AND_PRUNE_POSTS_SQL,
    SCHEMA_APPLY_RETRIES,
)

@pytest.fixture
def mock_psycopg_connect():
    mock_schema_file = MagicMock()
    mock_schema_file.read_text.return_value = "-- mock schema"
    
    with patch("db.psycopg.connect") as mock_connect, \
         patch("db.SCHEMA_FILE", mock_schema_file):
        
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        
        # Cursor mock
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        yield mock_connect, mock_conn, mock_cursor

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


def _schema_failing_side_effect(fail_times):
    """cursor.execute side effect: the bookkeeping statements around the apply —
    the advisory lock/unlock that serializes concurrent starters and the
    `SET lock_timeout` guard — all pass, but the schema apply itself raises
    DeadlockDetected the first `fail_times` times, then succeeds."""
    state = {"schema_calls": 0}

    def _side_effect(sql, *args, **kwargs):
        s = str(sql)
        if "lock_timeout" in s or "pg_advisory" in s:
            return None
        state["schema_calls"] += 1
        if state["schema_calls"] <= fail_times:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return None

    return _side_effect, state


def test_apply_schema_retries_then_succeeds(mock_psycopg_connect, monkeypatch):
    mock_connect, mock_conn, mock_cursor = mock_psycopg_connect
    # Don't let the mock context manager swallow the raised exception.
    mock_conn.cursor.return_value.__exit__.return_value = False
    monkeypatch.setattr("db.time.sleep", lambda _s: None)

    side_effect, state = _schema_failing_side_effect(fail_times=2)
    mock_cursor.execute.side_effect = side_effect

    # Two transient deadlocks should be retried, not crash startup.
    db = DB("mock_dsn")

    assert state["schema_calls"] == 3
    assert db is not None
    # The advisory lock that serializes concurrent starters is taken and released.
    executed = [str(c.args[0]) for c in mock_cursor.execute.call_args_list]
    assert any("pg_advisory_lock" in e for e in executed)
    assert any("pg_advisory_unlock" in e for e in executed)


def test_apply_schema_raises_after_exhausting_retries(mock_psycopg_connect, monkeypatch):
    mock_connect, mock_conn, mock_cursor = mock_psycopg_connect
    mock_conn.cursor.return_value.__exit__.return_value = False
    monkeypatch.setattr("db.time.sleep", lambda _s: None)

    side_effect, state = _schema_failing_side_effect(fail_times=999)
    mock_cursor.execute.side_effect = side_effect

    # A persistent deadlock surfaces after the bounded retries (doesn't loop forever).
    with pytest.raises(psycopg.errors.DeadlockDetected):
        DB("mock_dsn")

    assert state["schema_calls"] == SCHEMA_APPLY_RETRIES


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
    mock_cursor.execute.assert_called_once_with(
        ROLLUP_AND_PRUNE_POSTS_SQL,
        [POST_RETENTION_ADVISORY_LOCK_KEY, cutoff],
    )


def test_rollup_and_prune_sql_aggregates_exactly_the_deleted_rows():
    sql = " ".join(ROLLUP_AND_PRUNE_POSTS_SQL.lower().split())

    assert sql.count("delete from posts") == 1
    assert "where timestamp < %s" in sql
    assert "where timestamp <= %s" not in sql
    assert "returning symbol, timestamp, sentiment, engagement" in sql
    assert "from deleted_posts" in sql
    assert "date_trunc('hour', timestamp, 'utc')" in sql

    # Both deletion and aggregation are data-modifying CTEs in one statement;
    # there is no second prune statement that could observe a different snapshot.
    assert "with maintenance_lock as materialized" in sql
    assert "upserted_buckets as ( insert into hourly_sentiment_agg" in sql
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
    assert "exists (select 1 from maintenance_lock)" in sql
    # A stable upsert order further avoids conflicting bucket locks being taken
    # in different orders if maintenance implementations evolve.
    assert "from aggregated_posts order by symbol, bucket_hour" in sql


def test_rollup_and_prune_posts_retries_unknown_commit_safely(
    mock_psycopg_connect,
):
    mock_connect, _, mock_cursor = mock_psycopg_connect
    db = DB("mock_dsn")
    mock_cursor.reset_mock()
    mock_cursor.execute.side_effect = [
        psycopg.OperationalError("connection lost"),
        None,
    ]
    mock_cursor.fetchone.return_value = (1, 4)
    cutoff = datetime(2026, 7, 14, 16, tzinfo=timezone.utc)

    assert db.rollup_and_prune_posts(cutoff) == (1, 4)
    assert mock_cursor.execute.call_count == 2
    assert mock_connect.call_count == 2


def test_rollup_and_prune_posts_propagates_statement_failure(
    mock_psycopg_connect,
):
    _, mock_conn, mock_cursor = mock_psycopg_connect
    db = DB("mock_dsn")
    mock_cursor.reset_mock()
    mock_conn.cursor.return_value.__exit__.return_value = False
    mock_cursor.execute.side_effect = psycopg.errors.CheckViolation(
        "aggregate write failed"
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db.rollup_and_prune_posts(
            datetime(2026, 7, 14, 16, tzinfo=timezone.utc)
        )

    # No exception is swallowed and there is no follow-up DELETE. PostgreSQL
    # therefore rolls the data-modifying CTE statement back as one unit.
    mock_cursor.execute.assert_called_once()
