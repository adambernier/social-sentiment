import pytest
import psycopg
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from shared.schemas import StockQuote, StockMetrics
from db import DB, SCHEMA_APPLY_RETRIES

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
    """cursor.execute side effect: the `SET lock_timeout` passes, but the schema
    apply raises DeadlockDetected the first `fail_times` times, then succeeds."""
    state = {"schema_calls": 0}

    def _side_effect(sql, *args, **kwargs):
        if "lock_timeout" in str(sql):
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
