import asyncio
import importlib
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

# Import main from api-service using importlib
api_main = importlib.import_module("api-service.main")
app = api_main.app

@pytest.fixture
def mock_db():
    # Patch postgres_listener to avoid starting background DB listener tasks
    # and patch AsyncConnectionPool so it doesn't open real connections
    with patch.object(api_main, "postgres_listener", return_value=asyncio.sleep(0)) as mock_listener, \
         patch.object(api_main, "AsyncConnectionPool") as mock_pool_cls:
        
        mock_pool = MagicMock()
        mock_pool.open = AsyncMock()
        mock_pool.close = AsyncMock()
        mock_pool_cls.return_value = mock_pool
        
        # Setup mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = AsyncMock()  # This has async execute, fetchall, etc.
        
        # mock_conn.cursor() is a synchronous call returning an async context manager
        # Set return_value=False for __aexit__ to ensure exceptions are propagated instead of suppressed
        mock_conn.cursor = MagicMock()
        mock_conn.cursor.return_value.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)
        
        with patch.object(api_main, "get_db_conn") as mock_get_db_conn:
            # get_db_conn() is a synchronous call returning an async context manager
            mock_context = MagicMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_context.__aexit__ = AsyncMock(return_value=False)
            mock_get_db_conn.return_value = mock_context
            
            yield mock_cursor

def test_health_endpoint_healthy(mock_db):
    # Setup SELECT 1 to succeed
    mock_db.execute = AsyncMock()
    mock_db.fetchall = AsyncMock(return_value=[(1,)])
    
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "database": "connected"}

def test_health_endpoint_unhealthy(mock_db):
    # Setup DB query to fail
    mock_db.execute = AsyncMock(side_effect=Exception("Connection lost"))
    
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "unhealthy"
        assert "Connection lost" in response.json()["error"]

def test_posts_endpoint(mock_db):
    # Mock data that matches PostResponse fields
    now = datetime.now(timezone.utc)
    mock_posts = [
        {
            "id": "1",
            "symbol": "AAPL",
            "platform": "reddit",
            "text": "Apple is looking great",
            "timestamp": now.isoformat(),
            "sentiment": "positive",
            "scores": {"positive": 0.9, "neutral": 0.1, "negative": 0.0},
            "topic_id": 1,
            "topic_label": "financial earnings",
            "scored_at": now.isoformat(),
            "engagement": 10
        }
    ]
    mock_db.fetchall = AsyncMock(return_value=mock_posts)
    
    with TestClient(app) as client:
        response = client.get("/posts?symbol=AAPL")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "1"
        assert data[0]["symbol"] == "AAPL"
        assert data[0]["sentiment"] == "positive"

def test_sentiment_stats_endpoint(mock_db):
    mock_stats = [
        {"sentiment": "positive", "count": 15},
        {"sentiment": "negative", "count": 5}
    ]
    mock_db.fetchall = AsyncMock(return_value=mock_stats)
    
    with TestClient(app) as client:
        response = client.get("/stats/sentiment?symbol=AAPL")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["sentiment"] == "positive"
        assert data[0]["count"] == 15

def test_topic_stats_endpoint(mock_db):
    mock_topics = [
        {"topic_label": "Earnings & Guidance", "count": 8},
        {"topic_label": "AI & Compute", "count": 12}
    ]
    mock_db.fetchall = AsyncMock(return_value=mock_topics)
    
    with TestClient(app) as client:
        response = client.get("/stats/topics")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["topic_label"] == "Earnings & Guidance"
        assert data[0]["count"] == 8
