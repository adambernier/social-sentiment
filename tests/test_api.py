import asyncio
import importlib
import math
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

# Import main from api-service using importlib
api_main = importlib.import_module("api-service.main")
app = api_main.app


@pytest.mark.asyncio
async def test_websocket_broadcast_does_not_let_stalled_client_block_healthy_client(
):
    manager = api_main.ConnectionManager(send_timeout_seconds=0.5)
    healthy_sent = asyncio.Event()
    stalled_cancelled = asyncio.Event()

    async def stall_send(_message):
        try:
            await asyncio.Event().wait()
        finally:
            stalled_cancelled.set()

    async def healthy_send(_message):
        healthy_sent.set()

    stalled = MagicMock()
    stalled.send_text = AsyncMock(side_effect=stall_send)
    healthy = MagicMock()
    healthy.send_text = AsyncMock(side_effect=healthy_send)
    manager.active_connections = [stalled, healthy]

    broadcast = asyncio.create_task(manager.broadcast("new-post"))
    await asyncio.wait_for(healthy_sent.wait(), timeout=0.25)

    assert not broadcast.done()

    await asyncio.wait_for(broadcast, timeout=1)

    stalled.send_text.assert_awaited_once_with("new-post")
    healthy.send_text.assert_awaited_once_with("new-post")
    assert stalled_cancelled.is_set()
    assert manager.active_connections == [healthy]


@pytest.mark.asyncio
async def test_websocket_broadcast_removes_only_clients_with_send_errors(caplog):
    manager = api_main.ConnectionManager(send_timeout_seconds=0.2)
    failed = MagicMock()
    failed.send_text = AsyncMock(side_effect=RuntimeError("connection reset"))
    healthy = MagicMock()
    healthy.send_text = AsyncMock()
    manager.active_connections = [failed, healthy]

    await manager.broadcast("new-post")

    failed.send_text.assert_awaited_once_with("new-post")
    healthy.send_text.assert_awaited_once_with("new-post")
    assert manager.active_connections == [healthy]
    assert "connection reset" in caplog.text


@pytest.mark.asyncio
async def test_websocket_broadcast_propagates_cancellation_without_leaking_send_tasks():
    manager = api_main.ConnectionManager(send_timeout_seconds=10)
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    async def blocked_send(_message):
        send_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            send_cancelled.set()

    connection = MagicMock()
    connection.send_text = AsyncMock(side_effect=blocked_send)
    manager.active_connections = [connection]

    broadcast = asyncio.create_task(manager.broadcast("new-post"))
    await asyncio.wait_for(send_started.wait(), timeout=0.5)
    broadcast.cancel()

    with pytest.raises(asyncio.CancelledError):
        await broadcast

    await asyncio.wait_for(send_cancelled.wait(), timeout=0.5)
    assert broadcast.done()
    assert manager.active_connections == [connection]


@pytest.mark.asyncio
async def test_websocket_broadcast_uses_connection_snapshot():
    manager = api_main.ConnectionManager(send_timeout_seconds=0.2)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def delayed_send(_message):
        first_started.set()
        await release_first.wait()

    first = MagicMock()
    first.send_text = AsyncMock(side_effect=delayed_send)
    newcomer = MagicMock()
    newcomer.send_text = AsyncMock()
    manager.active_connections = [first]

    broadcast = asyncio.create_task(manager.broadcast("first-message"))
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    manager.active_connections.append(newcomer)
    release_first.set()
    await broadcast

    newcomer.send_text.assert_not_awaited()

    await manager.broadcast("second-message")

    first.send_text.assert_awaited_with("second-message")
    newcomer.send_text.assert_awaited_once_with("second-message")


@pytest.mark.asyncio
async def test_websocket_endpoint_disconnects_client_when_cancelled():
    manager = api_main.ConnectionManager(send_timeout_seconds=0.2)
    receive_started = asyncio.Event()
    receive_cancelled = asyncio.Event()

    async def blocked_receive():
        receive_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            receive_cancelled.set()

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.receive_text = AsyncMock(side_effect=blocked_receive)

    with patch.object(api_main, "manager", manager):
        handler = asyncio.create_task(api_main.websocket_endpoint(websocket))
        await asyncio.wait_for(receive_started.wait(), timeout=0.5)
        handler.cancel()

        with pytest.raises(asyncio.CancelledError):
            await handler

    await asyncio.wait_for(receive_cancelled.wait(), timeout=0.5)
    assert manager.active_connections == []


@pytest.fixture
def mock_db():
    # Patch postgres_listener to avoid starting background DB listener tasks
    # and patch startup resources so tests never open real connections.
    with patch.object(api_main, "postgres_listener", new_callable=AsyncMock) as mock_listener, \
         patch.object(api_main, "start_symbol_registry", new_callable=AsyncMock), \
         patch.object(api_main, "stop_symbol_registry", new_callable=AsyncMock), \
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
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "database": "connected"}

def test_health_endpoint_unhealthy(mock_db):
    # Setup DB query to fail
    mock_db.execute = AsyncMock(side_effect=Exception("Connection lost"))
    
    with TestClient(app) as client:
        response = client.get("/api/health")
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
        response = client.get("/api/posts?symbol=AAPL")
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
        response = client.get("/api/stats/sentiment?symbol=AAPL")
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
        response = client.get("/api/stats/topics")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["topic_label"] == "Earnings & Guidance"
        assert data[0]["count"] == 8


@pytest.mark.parametrize(
    ("hours", "expected_bucket_minutes"),
    [
        (1, 1),
        (24, 1),
        (36, 1),
        (37, 5),
        (168, 5),
        (549, 15),
        (550, 30),
        (720, 30),
        (2160, 60),
        (8760, 240),
    ],
)
def test_dashboard_market_bucket_size_bounds_response(
    hours,
    expected_bucket_minutes,
):
    bucket_minutes = api_main.dashboard_market_bucket_minutes(hours)

    assert bucket_minutes == expected_bucket_minutes
    assert math.ceil(hours * 60 / bucket_minutes) + 1 <= (
        api_main.DASHBOARD_MAX_MARKET_POINTS_PER_SYMBOL
    )


def test_dashboard_queries_batch_and_downsample_data():
    content_query = " ".join(api_main.DASHBOARD_CONTENT_QUERY.lower().split())
    series_query = " ".join(
        api_main.DASHBOARD_MARKET_SERIES_QUERY.lower().split()
    )
    snapshot_query = " ".join(
        api_main.DASHBOARD_MARKET_SNAPSHOT_QUERY.lower().split()
    )

    assert content_query.count("from posts") == 1
    assert "filtered_posts as materialized" in content_query
    assert "from filtered_posts" in content_query
    assert "date_bin(" in series_query
    assert "distinct on (symbol, bucket_start)" in series_query
    assert "symbol = any(%s)" in series_query
    assert snapshot_query.count("left join lateral") == 4
    assert "from unnest(%s::text[])" in snapshot_query
    assert "left join stock_metrics" in snapshot_query


@pytest.mark.asyncio
async def test_dashboard_batches_queries_and_preserves_response_contract(
    mock_db,
):
    now = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)
    content = {
        "posts": [
            {
                "id": "post-1",
                "symbol": "NVDA",
                "platform": "reddit",
                "text": "NVDA earnings",
                "timestamp": now,
                "sentiment": "positive",
                "scores": {"positive": 0.9},
                "topic_id": 1,
                "topic_label": "Earnings & Guidance",
                "scored_at": now,
                "engagement": 4,
            }
        ],
        "sentiment_stats": [{"sentiment": "positive", "count": 1}],
        "topic_stats": [
            {"topic_label": "Earnings & Guidance", "count": 1}
        ],
    }
    series_rows = [
        {
            "symbol": "NVDA",
            "timestamp": now - timedelta(minutes=5),
            "price": 109.0,
            "volume": 1000,
            "market_session": "regular",
        },
        {
            "symbol": "NQ=F",
            "timestamp": now - timedelta(minutes=5),
            "price": 204.0,
            "volume": 2000,
            "market_session": "futures_open",
        },
    ]
    snapshots = [
        {
            "symbol": "NVDA",
            "latest_timestamp": now,
            "latest_price": 110.0,
            "latest_volume": 1100,
            "latest_market_session": "regular",
            "reference_price": 100.0,
            "metrics_symbol": "NVDA",
            "pe_ratio": 25.0,
            "beta": 1.2,
            "avg_return_1y": 0.3,
            "inflation_adj_return_1y": 0.27,
            "pe_relative_sector": -0.1,
            "beta_relative_sector": -0.2,
            "return_relative_sector": 0.4,
            "updated_at": now,
        },
        {
            "symbol": "NQ=F",
            "latest_timestamp": now,
            "latest_price": 205.0,
            "latest_volume": 2100,
            "latest_market_session": "futures_open",
            "reference_price": 200.0,
            "metrics_symbol": None,
        },
        {
            "symbol": api_main.VIX_SYMBOL,
            "latest_timestamp": now,
            "latest_price": 18.0,
            "latest_volume": 500,
            "latest_market_session": "regular",
            "reference_price": 17.0,
            "metrics_symbol": None,
        },
    ]
    mock_db.fetchone = AsyncMock(return_value=content)
    mock_db.fetchall = AsyncMock(side_effect=[series_rows, snapshots])

    with patch.object(
        api_main,
        "primary_futures_map",
        return_value={"NVDA": "NQ=F"},
    ):
        response = await api_main.get_dashboard(
            symbol="NVDA",
            hours=168,
            platform="reddit",
        )

    validated = api_main.DashboardResponse.model_validate(response)
    assert validated.posts[0].id == "post-1"
    assert [quote.symbol for quote in validated.market_data] == ["NVDA"]
    assert [quote.symbol for quote in validated.primary_future_market_data] == [
        "NQ=F"
    ]
    assert validated.latest_quote.price == 110.0
    assert validated.primary_delta.pct_change == pytest.approx(10.0)
    assert validated.primary_future_delta.pct_change == pytest.approx(2.5)
    assert validated.vix_quote.price == 18.0
    assert validated.metrics_data.symbol == "NVDA"

    assert mock_db.execute.await_count == 3
    content_call, series_call, snapshot_call = mock_db.execute.call_args_list
    assert "AND platform = %s" in content_call.args[0]
    assert content_call.args[1][0] == "NVDA"
    assert content_call.args[1][2] == "reddit"
    assert series_call.args[0] == api_main.DASHBOARD_MARKET_SERIES_QUERY
    assert series_call.args[1][0] == 5
    assert series_call.args[1][1] == ["NVDA", "NQ=F"]
    assert series_call.args[1][2] == content_call.args[1][1]
    assert snapshot_call.args == (
        api_main.DASHBOARD_MARKET_SNAPSHOT_QUERY,
        [["NVDA", "NQ=F", api_main.VIX_SYMBOL]],
    )


@pytest.mark.asyncio
async def test_dashboard_without_future_keeps_nullable_contract(mock_db):
    mock_db.fetchone = AsyncMock(
        return_value={"posts": [], "sentiment_stats": [], "topic_stats": []}
    )
    mock_db.fetchall = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "symbol": "NVDA",
                    "latest_timestamp": None,
                    "latest_price": None,
                    "metrics_symbol": None,
                },
                {
                    "symbol": api_main.VIX_SYMBOL,
                    "latest_timestamp": None,
                    "latest_price": None,
                    "metrics_symbol": None,
                },
            ],
        ]
    )

    with patch.object(api_main, "primary_futures_map", return_value={}):
        response = await api_main.get_dashboard(
            symbol="NVDA",
            hours=24,
            platform=None,
        )

    validated = api_main.DashboardResponse.model_validate(response)
    assert validated.primary_future_symbol is None
    assert validated.primary_future_quote is None
    assert validated.primary_future_delta is None
    assert validated.primary_future_market_data == []
    assert validated.latest_quote is None
    assert validated.primary_delta is None
    assert validated.vix_quote is None
    assert validated.metrics_data is None

    content_call, series_call, snapshot_call = mock_db.execute.call_args_list
    assert "AND platform = %s" not in content_call.args[0]
    assert content_call.args[1][0] == "NVDA"
    assert len(content_call.args[1]) == 2
    assert series_call.args[1][1] == ["NVDA"]
    assert snapshot_call.args[1] == [["NVDA", api_main.VIX_SYMBOL]]

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
        response = client.get("/api/stats/leaderboard")
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

@pytest.mark.asyncio
async def test_correlation_endpoint(mock_db):
    now = datetime.now(timezone.utc)
    now_hour = now.replace(minute=0, second=0, microsecond=0)
    
    # Mock the rows returned by the database-side hourly resampling query.
    mock_hourly_rows = []
    for row_symbol, starting_price in (("NVDA", 100.0), ("NQ", 200.0)):
        for i in range(20):
            price = starting_price + i
            previous_price = starting_price + i - 1
            mock_hourly_rows.append({
                "symbol": row_symbol,
                "bucket_hour": now_hour - timedelta(hours=19 - i),
                "price": price,
                "price_change": (
                    None
                    if i == 0
                    else ((price - previous_price) / previous_price) * 100
                ),
                "is_market_open": True,
            })
    
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
        {
            "sentiment": "positive",
            "scores": {"positive": 1.0, "neutral": 0.0, "negative": 0.0},
            "timestamp": now_hour,
            "engagement": 10,
        }
    ]
    
    # Setup mock_db.fetchall side_effect to return mocks for sequential queries:
    # 1. combined target/future hourly rows
    # 2. agg sentiment
    # 3. live posts
    mock_db.fetchall.side_effect = [
        mock_hourly_rows,
        mock_aggs,
        mock_posts
    ]
    
    mock_db.fetchone = AsyncMock()
    mock_db.fetchone.side_effect = [
        {"coverage_start": now_hour - timedelta(hours=48)},
        {"market_session": "regular"},
        {"pe_relative_sector": -0.25}, # Undervalued relative to sector
        {"price": 12.5}               # Low VIX (options are cheap)
    ]
    
    with patch.object(api_main, "primary_futures_map", return_value={"NVDA": "NQ"}):
        data = await api_main.get_correlation(
            symbol="NVDA",
            hours=24,
            platform=None,
            topic=None,
        )
        assert "data" in data
        assert "closedRegions" in data
        assert "supportPrice" in data
        assert "resistancePrice" in data
        assert "maxR" in data
        assert "bestLag" in data
        assert "opportunity" in data
        assert data["coverageComplete"] is True
        assert data["coverageMode"] == "legacy-unfiltered+canonical"
        current_bucket = next(
            bucket
            for bucket in data["data"]
            if bucket["timestamp"]
            == now_hour.isoformat().replace("+00:00", "Z")
        )
        assert current_bucket["positive"] == 6
        assert current_bucket["totalWeighted"] == pytest.approx(
            8.0 + math.log1p(10)
        )

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

        # Spot and future symbols are resampled in one query, with one prior
        # cutoff hour requested to seed the first in-window hourly return.
        market_query_call = mock_db.execute.call_args_list[0]
        assert market_query_call.args[0] == api_main.HOURLY_MARKET_QUERY
        assert market_query_call.args[1] == [
            ["NVDA", "NQ"],
            now_hour - timedelta(hours=25),
            ["NVDA", "NQ"],
            now_hour - timedelta(hours=25),
        ]


def test_hourly_market_query_resamples_quotes_by_symbol_and_utc_hour():
    query = " ".join(api_main.HOURLY_MARKET_QUERY.lower().split())

    # The descending aggregate selects the final quote in each hour, not a
    # minute-level quote, and a mixed session hour is open if any row is regular.
    assert "(array_agg(price order by timestamp desc))[1]" in query
    assert "bool_or(market_session = 'regular')" in query
    assert "date_trunc('hour', timestamp, 'utc')" in query
    assert "from market_hourly_facts" in query

    # LAG is isolated per symbol and a return is emitted only for a genuinely
    # adjacent hour. This prevents both cross-symbol and missing-hour returns.
    assert query.count("partition by symbol order by bucket_hour") == 2
    assert "previous_hour = bucket_hour - interval '1 hour'" in query
    assert "where symbol = any(%s) and timestamp >= %s" in query


def test_filtered_cold_tier_query_never_uses_dimensionless_legacy_rows():
    cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)

    query, params, is_filtered = api_main._sentiment_aggregate_query(
        "NVDA",
        cutoff,
        "bluesky",
        "AI & Compute",
    )
    normalized = " ".join(query.lower().split())

    assert is_filtered is True
    assert "and platform = %s" in normalized
    assert "and topic_label = %s" in normalized
    assert "and %s::boolean = false" in normalized
    assert params == [
        "NVDA",
        cutoff,
        "bluesky",
        "AI & Compute",
        "NVDA",
        cutoff,
        True,
    ]


def test_filtered_coverage_uses_the_requested_dimensions_and_not_legacy():
    query, params = api_main._sentiment_coverage_query(
        "NVDA",
        "bluesky",
        "AI & Compute",
    )
    normalized = " ".join(query.lower().split())

    assert normalized.count("and platform = %s") == 2
    assert normalized.count("and topic_label = %s") == 2
    assert "hourly_sentiment_agg" not in normalized
    assert params == [
        "NVDA",
        "bluesky",
        "AI & Compute",
        "NVDA",
        "bluesky",
        "AI & Compute",
    ]


def test_unfiltered_coverage_can_include_legacy_history():
    query, params = api_main._sentiment_coverage_query(
        "NVDA",
        None,
        "all",
    )

    assert "hourly_sentiment_agg" in query
    assert params == ["NVDA", "NVDA", "NVDA"]


def test_apply_hourly_market_rows_preserves_gaps_sessions_and_symbols():
    hour = datetime(2026, 7, 20, 16, tzinfo=timezone.utc)

    def key(offset):
        return (hour + timedelta(hours=offset)).isoformat().replace("+00:00", "Z")

    buckets = {
        key(offset): {
            "priceChange": None,
            "futureChange": None,
            "futurePct": None,
            "rawPrice": None,
            "isMarketOpen": False,
        }
        for offset in range(3)
    }
    rows = [
        # Prior-cutoff anchor: absent from buckets, but its close seeds the
        # database-computed return on the first visible target/future bucket.
        {
            "symbol": "NVDA",
            "bucket_hour": hour - timedelta(hours=1),
            "price": 100.0,
            "price_change": None,
            "is_market_open": False,
        },
        {
            "symbol": "NQ",
            "bucket_hour": hour - timedelta(hours=1),
            "price": 200.0,
            "price_change": None,
            "is_market_open": False,
        },
        {
            "symbol": "NVDA",
            "bucket_hour": hour,
            "price": 102.0,
            "price_change": 2.0,
            "is_market_open": True,
        },
        {
            "symbol": "NQ",
            "bucket_hour": hour,
            "price": 202.0,
            "price_change": 1.0,
            "is_market_open": True,
        },
        # No rows for hour + 1. The gap return at hour + 2 must remain null.
        {
            "symbol": "NVDA",
            "bucket_hour": hour + timedelta(hours=2),
            "price": 104.0,
            "price_change": None,
            "is_market_open": False,
        },
        {
            "symbol": "NQ",
            "bucket_hour": hour + timedelta(hours=2),
            "price": 206.0,
            "price_change": None,
            "is_market_open": True,
        },
    ]

    market_data = api_main._apply_hourly_market_rows(
        buckets, rows, "NVDA", "NQ"
    )

    assert [row["symbol"] for row in market_data] == ["NVDA"] * 3
    assert buckets[key(0)]["rawPrice"] == 102.0
    assert buckets[key(0)]["priceChange"] == pytest.approx(2.0)
    assert buckets[key(0)]["isMarketOpen"] is True
    assert buckets[key(0)]["futureChange"] == pytest.approx(1.0)
    assert buckets[key(0)]["futurePct"] == pytest.approx(
        (202.0 - 206.0) / 206.0 * 100
    )

    assert buckets[key(1)]["rawPrice"] is None
    assert buckets[key(1)]["priceChange"] is None
    assert buckets[key(1)]["isMarketOpen"] is False

    assert buckets[key(2)]["rawPrice"] == 104.0
    assert buckets[key(2)]["priceChange"] is None
    assert buckets[key(2)]["isMarketOpen"] is False
    assert buckets[key(2)]["futureChange"] is None
    assert buckets[key(2)]["futurePct"] == pytest.approx(0.0)


def test_lagged_correlation_excludes_closed_session_returns():
    data = [
        {"sentimentIndex": sentiment, "priceChange": price, "isMarketOpen": True}
        for sentiment, price in zip(
            (1.0, 2.0, 3.0, 4.0),
            (2.0, 4.0, 6.0, 8.0),
        )
    ]
    # These closed-session values would materially distort the result if they
    # were included in the Pearson sample.
    data.extend([
        {"sentimentIndex": 5.0, "priceChange": 100.0, "isMarketOpen": False},
        {"sentimentIndex": 6.0, "priceChange": -100.0, "isMarketOpen": False},
        {"sentimentIndex": 7.0, "priceChange": 100.0, "isMarketOpen": False},
        {"sentimentIndex": 8.0, "priceChange": -100.0, "isMarketOpen": False},
    ])

    max_r, best_lag, sweeps = api_main.compute_lagged_correlation(data, max_lag=0)

    assert max_r == pytest.approx(1.0)
    assert best_lag == 0
    assert sweeps == [{"lag": 0, "r": pytest.approx(1.0)}]

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
        {"platform": "alpaca", "posts_1h": 0, "posts_24h": 10, "last_ingest": now - timedelta(hours=2)},
        # posted within the hour → active
        {"platform": "bluesky", "posts_1h": 5, "posts_24h": 200, "last_ingest": now - timedelta(minutes=2)},
        # finnhub absent from rows → no data in 24h → silent
    ]
    mock_db.fetchall = AsyncMock(return_value=mock_rows)
    mock_db.fetchone = AsyncMock(return_value=None)

    with TestClient(app) as client:
        response = client.get("/api/stats/sources")
        assert response.status_code == 200
        data = {d["platform"]: d for d in response.json()}
        assert data["stocktwits"]["status"] == "stalled"
        assert data["alpaca"]["status"] == "quiet"
        assert data["bluesky"]["status"] == "active"
        assert data["finnhub"]["status"] == "silent"
        # baseline rate is exposed for sources with volume, null when silent.
        assert data["stocktwits"]["baseline_per_hour"] == pytest.approx(1000 / 24)
        assert data["finnhub"]["baseline_per_hour"] is None
        # Problems (silent/stalled) are sorted to the top.
        assert response.json()[0]["status"] in ("silent", "stalled")
