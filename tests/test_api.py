import asyncio
import importlib
import sys
from datetime import datetime, timezone, timedelta
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

def test_correlation_endpoint(mock_db):
    now = datetime.now(timezone.utc)
    now_hour = now.replace(minute=0, second=0, microsecond=0)
    
    # Mock data for 20 quotes to correctly resolve 5th and 95th percentile indexes
    mock_quotes = [
        {"timestamp": now_hour - timedelta(hours=20-i), "price": 100.0 + i, "volume": 1000 + i * 10, "market_session": "regular"}
        for i in range(20)
    ]
    
    # Mock data for aggregates
    mock_aggs = [
        {
            "bucket_hour": now_hour,
            "positive_count": 5, "neutral_count": 2, "negative_count": 1,
            "positive_weighted": 5.0, "negative_weighted": 1.0, "neutral_weighted": 2.0,
            "total_weighted": 8.0, "sentiment_index": 0.5
        }
    ]
    
    # Mock data for live posts
    mock_posts = [
        {"sentiment": "positive", "timestamp": now_hour, "engagement": 10}
    ]
    
    # Setup mock_db.fetchall side_effect to return mocks for sequential queries:
    # 1. target quotes
    # 2. future quotes
    # 3. agg sentiment
    # 4. live posts
    mock_db.fetchall.side_effect = [
        mock_quotes,
        mock_quotes,
        mock_aggs,
        mock_posts
    ]
    
    mock_db.fetchone = AsyncMock(return_value={"market_session": "regular"})
    
    with TestClient(app) as client:
        response = client.get("/stats/correlation?symbol=NVDA&hours=2")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "closedRegions" in data
        assert "supportPrice" in data
        assert "resistancePrice" in data
        assert "maxR" in data
        assert "bestLag" in data
        
        # Support and resistance checks
        # support_price = sorted_prices[idx5] -> idx5 = int(19 * 0.05) = 0 -> 100.0
        # resistance_price = sorted_prices[idx95] -> idx95 = int(19 * 0.95) = 18 -> 118.0
        # latest_price = prices[-1] = 119.0
        assert data["supportPrice"] == 100.0
        assert data["resistancePrice"] == 118.0
        assert abs(data["supportPct"] - (-15.96638)) < 0.05
        assert abs(data["resistancePct"] - (-0.84033)) < 0.05
