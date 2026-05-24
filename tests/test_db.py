import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from shared.schemas import StockQuote, StockMetrics
from db import DB

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
