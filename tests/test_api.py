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
    
    mock_db.fetchone = AsyncMock()
    mock_db.fetchone.side_effect = [
        {"market_session": "regular"},
        {"pe_relative_sector": -0.25}, # Undervalued relative to sector
        {"price": 12.5}               # Low VIX (options are cheap)
    ]
    
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
        assert "opportunity" in data
        
        # Support and resistance checks
        # support_price = sorted_prices[idx5] -> idx5 = int(19 * 0.05) = 0 -> 100.0
        # resistance_price = sorted_prices[idx95] -> idx95 = int(19 * 0.95) = 18 -> 118.0
        assert data["supportPrice"] == 100.0
        assert data["resistancePrice"] == 118.0
        assert abs(data["supportPct"] - (-15.96638)) < 0.05
        assert abs(data["resistancePct"] - (-0.84033)) < 0.05

        # Opportunity scanner checks
        opp = data["opportunity"]
        assert opp is not None
        assert "score" in opp
        assert "classification" in opp
        assert "strategy" in opp
        assert "checklist" in opp
        
        # With price = 119.0 (latest quote), support = 100.0: Price is not near support.
        # Sentiment = 1.0 (positive weighted = 5.0, total weighted = 5.0): Bullish crossover.
        # Valuation = -0.25 (undervalued): +2.0 points.
        # Total score should be non-zero
        assert opp["score"] > 0

        # Check that historical buckets contain rawPrice, buySignal and buyScore
        first_bucket = data["data"][0]
        assert "rawPrice" in first_bucket
        assert "buySignal" in first_bucket
        assert "buyScore" in first_bucket

def test_compute_opportunity_logic():
    # Test compute_opportunity helper function directly
    compute_opp = getattr(api_main, "compute_opportunity")
    
    # 1. Test Strong Buy Setup with Low VIX -> Buy Call Option
    opp_strong_low_vix = compute_opp(
        price=101.0,
        support=100.0, # Within 2.5% of support
        resistance=110.0,
        sentiment=0.8,
        sentiment_sma=0.2, # Bullish sentiment crossover
        vix_price=12.0,    # Low VIX
        pe_relative=-0.3,  # Undervalued
        prev_prices=[105.0, 103.0, 101.0],      # Price down
        prev_sentiments=[0.2, 0.5, 0.8]        # Sentiment up (divergence)
    )
    assert opp_strong_low_vix["classification"] == "STRONG BUY"
    assert opp_strong_low_vix["score"] >= 75.0
    assert opp_strong_low_vix["strategy"] == "Long Call Option (Buy Call)"
    assert "Price near support level" in opp_strong_low_vix["checklist"]
    assert "Bullish sentiment crossover (above positive SMA)" in opp_strong_low_vix["checklist"]
    assert "Bullish sentiment divergence (price down, sentiment rising)" in opp_strong_low_vix["checklist"]

    # 2. Test Strong Buy Setup with High VIX -> Sell Put Credit Spreads
    opp_strong_high_vix = compute_opp(
        price=101.0,
        support=100.0,
        resistance=110.0,
        sentiment=0.8,
        sentiment_sma=0.2,
        vix_price=25.0,    # High VIX
        pe_relative=-0.3,
        prev_prices=[105.0, 103.0, 101.0],
        prev_sentiments=[0.2, 0.5, 0.8]
    )
    assert opp_strong_high_vix["classification"] == "STRONG BUY"
    assert opp_strong_high_vix["strategy"] == "Sell Put Credit Spreads"

    # 3. Test Caution / Overbought Setup
    opp_overbought = compute_opp(
        price=109.0,
        support=100.0,
        resistance=110.0, # Near resistance
        sentiment=-0.5,
        sentiment_sma=-0.1, # Bearish setup
        vix_price=15.0,
        pe_relative=0.5
    )
    assert opp_overbought["classification"] == "CAUTION / OVERBOUGHT"
    assert opp_overbought["strategy"] == "Protect Longs / Buy Puts"

