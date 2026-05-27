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
    with patch.object(api_main, "postgres_listener", new_callable=AsyncMock) as mock_listener, \
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

def test_leaderboard_endpoint(mock_db):
    # The endpoint runs a single query and returns the rows as-is; the SQL itself
    # (time-matched z-score, cold/hot baseline union, all-symbols seed) is validated
    # against a live DB, so here we confirm the wiring + response_model coercion,
    # including the nullable buzz_z for a too-sparse baseline.
    mock_rows = [
        {"symbol": "IREN", "post_count_4h": 60, "sentiment_index_4h": 0.58, "buzz_z": 2.40, "baseline_hourly": 5.0, "baseline_samples": 13},
        {"symbol": "NVDA", "post_count_4h": 137, "sentiment_index_4h": 0.08, "buzz_z": -0.80, "baseline_hourly": 144.0, "baseline_samples": 29},
        {"symbol": "INTC", "post_count_4h": 0, "sentiment_index_4h": 0.0, "buzz_z": None, "baseline_hourly": 0.0, "baseline_samples": 0},
    ]
    mock_db.execute = AsyncMock()
    mock_db.fetchall = AsyncMock(return_value=mock_rows)

    with TestClient(app) as client:
        response = client.get("/stats/leaderboard")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        # Columns map onto the model fields.
        assert set(data[0].keys()) == {"symbol", "post_count_4h", "sentiment_index_4h", "buzz_z", "baseline_hourly", "baseline_samples"}
        assert data[0]["symbol"] == "IREN"
        assert data[0]["buzz_z"] == pytest.approx(2.40)
        # A symbol with too few baseline samples returns None (not 0.0) for buzz_z.
        assert data[2]["symbol"] == "INTC"
        assert data[2]["buzz_z"] is None
        assert data[2]["post_count_4h"] == 0
        # The endpoint seeds the universe from tracked symbols and passes the
        # baseline-window and min-baseline-hours guards as query params (in order).
        called_args = mock_db.execute.call_args[0]
        assert called_args[1] == [
            api_main.tickers(),
            api_main.LEADERBOARD_BASELINE_DAYS,
            api_main.LEADERBOARD_MIN_BASELINE_HOURS,
        ]

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
    
    with patch.object(api_main, "primary_futures_map", return_value={"NVDA": "NQ"}), \
         TestClient(app) as client:
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

        # Lag sweep: one Pearson r per integer lag across the ±5h window, and
        # the reported bestLag must be one of the swept lags.
        assert "lagSweeps" in data
        sweeps = data["lagSweeps"]
        assert [s["lag"] for s in sweeps] == list(range(-5, 6))
        assert all(-1.0 <= s["r"] <= 1.0 for s in sweeps)
        assert data["bestLag"] in [s["lag"] for s in sweeps]
        # bestLag/maxR must agree with the strongest-magnitude swept r.
        peak = max(sweeps, key=lambda s: abs(s["r"]))
        assert abs(data["maxR"]) == abs(peak["r"])
        
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


def test_sentiment_macd_dense_and_warmup():
    # Pure helper; small periods so values are hand-checkable.
    macd = getattr(api_main, "compute_sentiment_macd")
    indices = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    res = macd(indices, fast=2, slow=4, signal=2)

    assert len(res) == 6
    # Warm-up: macd is None until the slow SMA fills (index 3).
    assert res[0]["macd"] is None
    assert res[2]["macd"] is None
    assert res[3]["macd"] == pytest.approx(0.10)
    assert res[5]["macd"] == pytest.approx(0.10)
    # Signal needs two consecutive macd values, so it is None at index 3.
    assert res[3]["signal"] is None
    assert res[3]["hist"] is None
    # Steady ramp -> macd flat -> histogram converges to zero.
    assert res[4]["signal"] == pytest.approx(0.10)
    assert res[4]["hist"] == pytest.approx(0.0)


def test_sentiment_macd_histogram_warmup_window():
    # First (slow-1)+(signal-1) histogram entries are None; here that is 4.
    macd = getattr(api_main, "compute_sentiment_macd")
    res = macd([0.1, 0.2, 0.3, 0.4, 0.5, 0.6], fast=2, slow=4, signal=2)
    for i in range(4):
        assert res[i]["hist"] is None, f"expected None hist at warm-up index {i}"
    assert res[4]["hist"] is not None


def test_sentiment_macd_empty_buckets_carry_forward():
    # Empty hours (None) must be carried forward, not treated as 0.0.
    macd = getattr(api_main, "compute_sentiment_macd")

    # A gapped series equals the same series with gaps filled by the prior value.
    dense = macd([0.5, 0.5, 0.5, 0.5, 0.5, 0.5], fast=2, slow=4, signal=2)
    gapped = macd([0.5, None, 0.5, None, 0.5, None], fast=2, slow=4, signal=2)
    assert gapped == dense

    # Burst-after-quiet on a constant series must NOT manufacture a cross:
    # if None were read as 0.0, the histogram would swing here.
    res = macd([0.8, 0.8, 0.8, None, None, 0.8, 0.8, 0.8], fast=2, slow=4, signal=2)
    for r in res:
        if r["hist"] is not None:
            assert r["hist"] == pytest.approx(0.0)


def test_sentiment_macd_leading_empties_stay_none():
    macd = getattr(api_main, "compute_sentiment_macd")
    res = macd([None, None, 0.5, 0.6, 0.7, 0.8], fast=2, slow=4, signal=2)
    assert res[0]["macd"] is None
    assert res[1]["macd"] is None
    # First fully-populated slow window lands at index 5.
    assert res[5]["macd"] == pytest.approx(0.10)
    # Signal never gets two consecutive macd values, so no histogram bars.
    assert all(r["hist"] is None for r in res)


def test_sentiment_macd_empty_input():
    macd = getattr(api_main, "compute_sentiment_macd")
    assert macd([], fast=2, slow=4, signal=2) == []


def test_source_health_volume_aware_status(mock_db):
    # Status is judged per-source: a busy source silent for 2h is "stalled", while a
    # slow source with the same gap is within its normal cadence ("quiet").
    now = datetime.now(timezone.utc)
    mock_rows = [
        # ~42/hr but silent 2h → gap far exceeds its average → stalled
        {"platform": "stocktwits", "posts_1h": 0, "posts_24h": 1000, "last_ingest": now - timedelta(hours=2)},
        # ~0.4/hr; a 2h gap is well within its slow cadence → quiet (not alarming)
        {"platform": "reddit", "posts_1h": 0, "posts_24h": 10, "last_ingest": now - timedelta(hours=2)},
        # posted within the hour → active
        {"platform": "bluesky", "posts_1h": 5, "posts_24h": 200, "last_ingest": now - timedelta(minutes=2)},
        # finnhub absent from rows → no data in 24h → silent
    ]
    mock_db.fetchall = AsyncMock(return_value=mock_rows)

    with TestClient(app) as client:
        response = client.get("/stats/sources")
        assert response.status_code == 200
        data = {d["platform"]: d for d in response.json()}
        assert data["stocktwits"]["status"] == "stalled"
        assert data["reddit"]["status"] == "quiet"
        assert data["bluesky"]["status"] == "active"
        assert data["finnhub"]["status"] == "silent"
        # baseline rate is exposed for sources with volume, null when silent.
        assert data["stocktwits"]["baseline_per_hour"] == pytest.approx(1000 / 24)
        assert data["finnhub"]["baseline_per_hour"] is None
        # Problems (silent/stalled) are sorted to the top.
        assert response.json()[0]["status"] in ("silent", "stalled")

