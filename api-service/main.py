import sys
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import DATABASE_DSN
from shared.symbols import primary_futures_map


# Global pool instance
db_pool: Optional[AsyncConnectionPool] = None

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

async def postgres_listener():
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(DATABASE_DSN, autocommit=True)
            async with conn:
                async with conn.cursor() as cur:
                    await cur.execute("LISTEN new_posts;")
                
                print("Listening for new_posts channel notifications...")
                async for notify in conn.notifies():
                    await manager.broadcast(notify.payload)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Postgres listener error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    print("Initializing async database connection pool...")
    db_pool = AsyncConnectionPool(
        DATABASE_DSN,
        min_size=5,
        max_size=20,
        open=False,
        kwargs={"row_factory": psycopg.rows.dict_row}
    )
    await db_pool.open()
    
    listener_task = asyncio.create_task(postgres_listener())
    
    yield
    
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass
        
    print("Closing async database connection pool...")
    await db_pool.close()

app = FastAPI(title="Social Sentiment API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PostResponse(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime
    sentiment: str
    scores: dict[str, float]
    topic_id: Optional[int]
    topic_label: Optional[str]
    scored_at: datetime
    engagement: int = 1

class TopicStats(BaseModel):
    topic_label: Optional[str]
    count: int

class SentimentStats(BaseModel):
    sentiment: str
    count: int

class MarketQuote(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    volume: int
    market_session: str

class MarketDelta(BaseModel):
    symbol: str
    reference_price: float
    latest_price: float
    pct_change: float
    abs_change: float

class StockMetricsResponse(BaseModel):
    symbol: str
    pe_ratio: Optional[float]
    beta: Optional[float]
    avg_return_1y: Optional[float]
    inflation_adj_return_1y: Optional[float]
    pe_relative_sector: Optional[float]
    beta_relative_sector: Optional[float]
    return_relative_sector: Optional[float]
    updated_at: datetime


class DashboardResponse(BaseModel):
    sentiment_stats: list[SentimentStats]
    topic_stats: list[TopicStats]
    posts: list[PostResponse]
    market_data: list[MarketQuote]
    latest_quote: Optional[MarketQuote]
    metrics_data: Optional[StockMetricsResponse]
    primary_delta: Optional[MarketDelta]
    
    primary_future_symbol: Optional[str]
    primary_future_quote: Optional[MarketQuote]
    primary_future_delta: Optional[MarketDelta]
    primary_future_market_data: list[MarketQuote]
    
    vix_quote: Optional[MarketQuote]
    vix_delta: Optional[MarketDelta]


class CorrelationBucket(BaseModel):
    timestamp: str
    positive: int
    neutral: int
    negative: int
    priceChange: Optional[float] = None
    futureChange: Optional[float] = None
    isMarketOpen: bool
    sentimentIndex: float
    sentimentSMA: float


class ClosedRegion(BaseModel):
    start: str
    end: str


class CorrelationResponse(BaseModel):
    data: list[CorrelationBucket]
    closedRegions: list[ClosedRegion]
    supportPrice: float
    supportPct: float
    resistancePrice: float
    resistancePct: float
    maxR: float
    bestLag: int
    correlationText: str
    correlationStrength: str


def get_db_conn():
    if db_pool is None:
        raise RuntimeError("Database pool not initialized")
    return db_pool.connection()

@app.get("/posts", response_model=list[PostResponse])
async def get_posts(
    symbol: Optional[str] = None,
    platform: Optional[str] = None,
    sentiment: Optional[str] = None,
    limit: int = Query(20, le=1000),
    offset: int = 0
):
    query = "SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at, engagement FROM posts"
    conditions = []
    params = []

    if symbol:
        conditions.append("symbol = %s")
        params.append(symbol)
    if platform:
        conditions.append("platform = %s")
        params.append(platform)
    if sentiment:
        conditions.append("sentiment = %s")
        params.append(sentiment)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

@app.get("/stats/sentiment", response_model=list[SentimentStats])
async def get_sentiment_stats(
    symbol: Optional[str] = None,
    platform: Optional[str] = None,
    hours: int = Query(24, gt=0)
):
    query = """
        SELECT sentiment, COUNT(*) as count 
        FROM posts 
        WHERE timestamp > %s
    """
    params = [datetime.now() - timedelta(hours=hours)]

    if symbol:
        query += " AND symbol = %s"
        params.append(symbol)
    if platform:
        query += " AND platform = %s"
        params.append(platform)

    query += " GROUP BY sentiment"

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

@app.get("/stats/topics", response_model=list[TopicStats])
async def get_topic_stats(
    symbol: Optional[str] = None,
    platform: Optional[str] = None,
    hours: int = Query(24, gt=0)
):
    query = """
        SELECT topic_label, COUNT(*) as count 
        FROM posts 
        WHERE timestamp > %s
    """
    params = [datetime.now() - timedelta(hours=hours)]

    if symbol:
        query += " AND symbol = %s"
        params.append(symbol)
    if platform:
        query += " AND platform = %s"
        params.append(platform)

    query += " GROUP BY topic_label ORDER BY count DESC"

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

@app.get("/stats/market", response_model=list[MarketQuote])
async def get_market_stats(
    symbol: str,
    hours: int = Query(24, gt=0)
):
    query = """
        SELECT symbol, timestamp, price, volume, market_session 
        FROM stock_quotes 
        WHERE symbol = %s AND timestamp > %s
        ORDER BY timestamp ASC
    """
    params = [symbol, datetime.now() - timedelta(hours=hours)]

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

@app.get("/stats/market/latest", response_model=Optional[MarketQuote])
async def get_latest_market_quote(symbol: str):
    query = """
        SELECT symbol, timestamp, price, volume, market_session 
        FROM stock_quotes 
        WHERE symbol = %s 
        ORDER BY timestamp DESC LIMIT 1
    """
    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, [symbol])
            return await cur.fetchone()

@app.get("/stats/market/delta", response_model=Optional[MarketDelta])
async def get_market_delta(
    symbol: str,
    since: datetime = Query(...)
):
    """
    Calculate pct_change for a symbol since a specific reference timestamp.
    Useful for 'normalization' of futures vs. last cash close.
    """
    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            # 1. Get latest price
            await cur.execute(
                "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )
            latest = await cur.fetchone()
            if not latest:
                return None
            
            latest_price = latest['price']

            # 2. Get reference price (the one closest to 'since' but not after it)
            await cur.execute(
                "SELECT price FROM stock_quotes WHERE symbol = %s AND timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
                [symbol, since]
            )
            ref = await cur.fetchone()
            
            # Fallback: if no quote exists before 'since', get the first one available
            if not ref:
                await cur.execute(
                    "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp ASC LIMIT 1",
                    [symbol]
                )
                ref = await cur.fetchone()

            if not ref:
                return None
            
            ref_price = ref['price']
            
            pct_change = 0.0
            abs_change = latest_price - ref_price
            if ref_price != 0:
                pct_change = (abs_change) / ref_price * 100

            return {
                "symbol": symbol,
                "reference_price": ref_price,
                "latest_price": latest_price,
                "pct_change": pct_change,
                "abs_change": abs_change
            }

@app.get("/stats/metrics", response_model=Optional[StockMetricsResponse])
async def get_stock_metrics(symbol: str):
    query = """
        SELECT symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y,
               pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at
        FROM stock_metrics 
        WHERE symbol = %s
    """
    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, [symbol])
            return await cur.fetchone()

@app.get("/stats/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    symbol: str,
    hours: int = Query(24, gt=0),
    platform: Optional[str] = None
):
    from datetime import timezone
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    futures_map = primary_futures_map()
    primary_future_symbol = futures_map.get(symbol)
    vix_symbol = "VX=F"
    
    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            # Helper to run query and fetch all rows
            async def run_query(q, p):
                await cur.execute(q, p)
                return await cur.fetchall()

            # Helper to run query and fetch one row
            async def run_query_one(q, p):
                await cur.execute(q, p)
                return await cur.fetchone()

            # Time threshold for hourly stats and graphs
            cutoff = datetime.now() - timedelta(hours=hours)

            # 1. Fetch posts matching symbol and optional platform, ordered by timestamp
            posts_query = (
                "SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at, engagement "
                "FROM posts "
                "WHERE symbol = %s AND timestamp >= %s"
            )
            posts_params = [symbol, cutoff]
            if platform:
                posts_query += " AND platform = %s"
                posts_params.append(platform)
            posts_query += " ORDER BY timestamp DESC LIMIT 1000"
            posts = await run_query(posts_query, posts_params)

            # 2. Fetch sentiment stats
            sent_query = (
                "SELECT sentiment, COUNT(*) as count "
                "FROM posts "
                "WHERE timestamp > %s AND symbol = %s"
            )
            sent_params = [cutoff, symbol]
            if platform:
                sent_query += " AND platform = %s"
                sent_params.append(platform)
            sent_query += " GROUP BY sentiment"
            sentiment_stats = await run_query(sent_query, sent_params)

            # 3. Fetch topic stats
            topic_query = (
                "SELECT topic_label, COUNT(*) as count "
                "FROM posts "
                "WHERE timestamp > %s AND symbol = %s"
            )
            topic_params = [cutoff, symbol]
            if platform:
                topic_query += " AND platform = %s"
                topic_params.append(platform)
            topic_query += " GROUP BY topic_label ORDER BY count DESC"
            topic_stats = await run_query(topic_query, topic_params)

            # 4. Fetch market data (quotes)
            market_query = (
                "SELECT symbol, timestamp, price, volume, market_session "
                "FROM stock_quotes "
                "WHERE symbol = %s AND timestamp > %s "
                "ORDER BY timestamp ASC"
            )
            market_data = await run_query(market_query, [symbol, cutoff])

            # 5. Fetch latest quote for target
            latest_quote = await run_query_one(
                "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )

            # 6. Fetch target metrics
            metrics_data = await run_query_one(
                "SELECT symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y, "
                "       pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at "
                "FROM stock_metrics WHERE symbol = %s",
                [symbol]
            )

            # Delta helper - change since previous close
            async def get_delta_data(sym, ref_time=None):
                await cur.execute(
                    "SELECT timestamp, price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                    [sym]
                )
                latest = await cur.fetchone()
                if not latest:
                    return None
                latest_price = latest['price']
                latest_ts = latest['timestamp']

                session_type = 'futures_open' if sym.endswith("=F") else 'regular'

                # Get the last regular/open session quote
                await cur.execute(
                    "SELECT timestamp, price FROM stock_quotes WHERE symbol = %s AND market_session = %s ORDER BY timestamp DESC LIMIT 1",
                    [sym, session_type]
                )
                latest_reg = await cur.fetchone()
                if not latest_reg:
                    # Fallback to oldest overall quote
                    await cur.execute(
                        "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp ASC LIMIT 1",
                        [sym]
                    )
                    ref = await cur.fetchone()
                    ref_price = ref['price'] if ref else latest_price
                else:
                    latest_reg_ts = latest_reg['timestamp']
                    # Previous close is the last regular/open session quote before the current trading session day started (at least 12 hours prior)
                    await cur.execute(
                        "SELECT price FROM stock_quotes WHERE symbol = %s AND market_session = %s AND timestamp < %s - %s::interval ORDER BY timestamp DESC LIMIT 1",
                        [sym, session_type, latest_reg_ts, timedelta(hours=12)]
                    )
                    ref = await cur.fetchone()
                    ref_price = ref['price'] if ref else latest_reg['price']

                abs_change = latest_price - ref_price
                pct_change = (abs_change / ref_price * 100) if ref_price != 0 else 0.0
                return {
                    "symbol": sym,
                    "reference_price": ref_price,
                    "latest_price": latest_price,
                    "pct_change": pct_change,
                    "abs_change": abs_change
                }

            primary_delta = await get_delta_data(symbol, since)

            # 7. Fetch primary future details if available
            p_future_quote = None
            p_future_delta = None
            p_future_market_data = []
            if primary_future_symbol:
                p_future_quote = await run_query_one(
                    "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                    [primary_future_symbol]
                )
                p_future_delta = await get_delta_data(primary_future_symbol, since)
                p_future_market_data = await run_query(market_query, [primary_future_symbol, cutoff])

            # 8. Fetch VIX details
            vix_quote = await run_query_one(
                "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [vix_symbol]
            )
            vix_delta = await get_delta_data(vix_symbol, since)

            return {
                "sentiment_stats": sentiment_stats,
                "topic_stats": topic_stats,
                "posts": posts,
                "market_data": market_data,
                "latest_quote": latest_quote,
                "metrics_data": metrics_data,
                "primary_delta": primary_delta,
                "primary_future_symbol": primary_future_symbol,
                "primary_future_quote": p_future_quote,
                "primary_future_delta": p_future_delta,
                "primary_future_market_data": p_future_market_data,
                "vix_quote": vix_quote,
                "vix_delta": vix_delta
            }

@app.get("/stats/correlation", response_model=CorrelationResponse)
async def get_correlation(
    symbol: str,
    hours: int = Query(24, gt=0),
    platform: Optional[str] = None,
    topic: Optional[str] = None
):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    futures_map = primary_futures_map()
    primary_future_symbol = futures_map.get(symbol)
    
    # Pre-fill hourly buckets to avoid missing hours
    buckets = {}
    for i in range(hours, -1, -1):
        bucket_time = (now - timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
        ts_str = bucket_time.isoformat().replace("+00:00", "Z")
        buckets[ts_str] = {
            "timestamp": ts_str,
            "positive": 0,
            "neutral": 0,
            "negative": 0,
            "priceChange": None,
            "futureChange": None,
            "isMarketOpen": False,
            "positiveWeighted": 0.0,
            "neutralWeighted": 0.0,
            "negativeWeighted": 0.0,
            "totalWeighted": 0.0,
            "sentimentIndex": 0.0,
            "sentimentSMA": 0.0
        }

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            # 1. Fetch market quotes for target
            market_query = """
                SELECT timestamp, price, volume, market_session 
                FROM stock_quotes 
                WHERE symbol = %s AND timestamp >= %s
                ORDER BY timestamp ASC
            """
            await cur.execute(market_query, [symbol, cutoff])
            market_data = await cur.fetchall()
            
            ref_price = market_data[0]['price'] if market_data else None
            for q in market_data:
                q_ts = q['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                q_ts_str = q_ts.isoformat().replace("+00:00", "Z")
                if q_ts_str in buckets:
                    if ref_price and ref_price != 0:
                        buckets[q_ts_str]['priceChange'] = ((q['price'] - ref_price) / ref_price) * 100
                    else:
                        buckets[q_ts_str]['priceChange'] = 0.0
                    buckets[q_ts_str]['isMarketOpen'] = True

            # 2. Fetch market quotes for primary future
            if primary_future_symbol:
                await cur.execute(market_query, [primary_future_symbol, cutoff])
                future_market_data = await cur.fetchall()
                ref_future_price = future_market_data[0]['price'] if future_market_data else None
                for q in future_market_data:
                    q_ts = q['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                    q_ts_str = q_ts.isoformat().replace("+00:00", "Z")
                    if q_ts_str in buckets:
                        if ref_future_price and ref_future_price != 0:
                            buckets[q_ts_str]['futureChange'] = ((q['price'] - ref_future_price) / ref_future_price) * 100
                        else:
                            buckets[q_ts_str]['futureChange'] = 0.0

            # 3. Fetch posts data
            posts_query = """
                SELECT sentiment, timestamp, engagement
                FROM posts
                WHERE symbol = %s AND timestamp >= %s
            """
            posts_params = [symbol, cutoff]
            if platform:
                posts_query += " AND platform = %s"
                posts_params.append(platform)
            if topic and topic != "all":
                posts_query += " AND topic_label = %s"
                posts_params.append(topic)
                
            await cur.execute(posts_query, posts_params)
            posts_data = await cur.fetchall()
            
            for p in posts_data:
                p_ts = p['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                p_ts_str = p_ts.isoformat().replace("+00:00", "Z")
                if p_ts_str in buckets:
                    weight = p['engagement'] if p['engagement'] is not None else 1
                    sent = p['sentiment']
                    if sent == 'positive':
                        buckets[p_ts_str]['positive'] += 1
                        buckets[p_ts_str]['positiveWeighted'] += weight
                    elif sent == 'negative':
                        buckets[p_ts_str]['negative'] += 1
                        buckets[p_ts_str]['negativeWeighted'] += weight
                    else:
                        buckets[p_ts_str]['neutral'] += 1
                        buckets[p_ts_str]['neutralWeighted'] += weight
                    buckets[p_ts_str]['totalWeighted'] += weight

            # 4. Fetch latest quote for regular session check
            await cur.execute(
                "SELECT market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )
            latest_quote = await cur.fetchone()

    # Post-process sentiment indices
    for b in buckets.values():
        if b['totalWeighted'] > 0:
            b['sentimentIndex'] = (b['positiveWeighted'] - b['negativeWeighted']) / b['totalWeighted']
        else:
            b['sentimentIndex'] = 0.0

    sorted_data = sorted(buckets.values(), key=lambda x: x['timestamp'])

    # 5. Calculate SMA
    sma_period = 5
    if hours <= 1:
        sma_period = 3
    elif hours <= 24:
        sma_period = 5
    elif hours <= 168:
        sma_period = 12
    elif hours <= 720:
        sma_period = 24

    for i in range(len(sorted_data)):
        sum_sma = 0.0
        count_sma = 0
        start_idx = max(0, i - sma_period + 1)
        for j in range(start_idx, i + 1):
            sum_sma += sorted_data[j]['sentimentIndex']
            count_sma += 1
        sorted_data[i]['sentimentSMA'] = sum_sma / count_sma if count_sma > 0 else 0.0

    # 6. Calculate Pearson Correlation
    def pearson_r(s_list, p_list):
        n = len(s_list)
        if n < 4:
            return 0.0
        mean_s = sum(s_list) / n
        mean_p = sum(p_list) / n
        num = sum((s - mean_s) * (p - mean_p) for s, p in zip(s_list, p_list))
        den_s = sum((s - mean_s) ** 2 for s in s_list)
        den_p = sum((p - mean_p) ** 2 for p in p_list)
        den = (den_s * den_p) ** 0.5
        return num / den if den > 0 else 0.0

    max_r = 0.0
    best_lag = 0
    for lag in range(-5, 6):
        s_vals = []
        p_vals = []
        for i in range(len(sorted_data)):
            p_idx = i + lag
            if 0 <= p_idx < len(sorted_data):
                s_val = sorted_data[i]['sentimentIndex']
                p_val = sorted_data[p_idx]['priceChange']
                if s_val is not None and p_val is not None:
                    s_vals.append(s_val)
                    p_vals.append(p_val)
        r = pearson_r(s_vals, p_vals)
        if abs(r) > abs(max_r):
            max_r = r
            best_lag = lag

    # Correlation Strength & Text
    correlation_strength = "weak"
    correlation_text = "No correlation detected"
    if abs(max_r) >= 0.15:
        if abs(max_r) > 0.6:
            correlation_strength = "strong"
        elif abs(max_r) > 0.35:
            correlation_strength = "moderate"
        
        relationship = "positive" if max_r >= 0 else "negative"
        sign = "+" if max_r >= 0 else ""
        if best_lag > 0:
            correlation_text = f"Sentiment leads price by {best_lag}h ({relationship}, r = {sign}{max_r:.2f})"
        elif best_lag < 0:
            correlation_text = f"Price leads sentiment by {abs(best_lag)}h ({relationship}, r = {sign}{max_r:.2f})"
        else:
            correlation_text = f"Coincident correlation ({relationship}, r = {sign}{max_r:.2f})"
    else:
        sign = "+" if max_r >= 0 else ""
        correlation_text = f"Weak or no correlation (r = {sign}{max_r:.2f})"

    # 7. Closed regions
    closed_regions = []
    current_start = None
    for idx, d in enumerate(sorted_data):
        if not d['isMarketOpen']:
            if not current_start:
                current_start = d['timestamp']
        else:
            if current_start:
                closed_regions.append({
                    "start": current_start,
                    "end": sorted_data[idx - 1]['timestamp']
                })
                current_start = None
    if current_start:
        closed_regions.append({
            "start": current_start,
            "end": sorted_data[-1]['timestamp']
        })

    if latest_quote and latest_quote.get('market_session') == 'regular' and len(closed_regions) > 0:
        if closed_regions[-1]['end'] == sorted_data[-1]['timestamp']:
            closed_regions.pop()

    actual_closed_regions = []
    for r in closed_regions:
        t_start = datetime.fromisoformat(r['start'].replace('Z', '+00:00'))
        t_end = datetime.fromisoformat(r['end'].replace('Z', '+00:00'))
        if (t_end - t_start) >= timedelta(hours=8):
            actual_closed_regions.append(r)

    # 8. Support & Resistance
    prices = [q['price'] for q in market_data if q['price'] is not None]
    support_price = 0.0
    resistance_price = 0.0
    support_pct = 0.0
    resistance_pct = 0.0
    if len(prices) > 0:
        sorted_prices = sorted(prices)
        idx5 = int((len(sorted_prices) - 1) * 0.05)
        idx95 = int((len(sorted_prices) - 1) * 0.95)
        support_price = sorted_prices[idx5]
        resistance_price = sorted_prices[idx95]
        if ref_price and ref_price != 0:
            support_pct = ((support_price - ref_price) / ref_price) * 100
            resistance_pct = ((resistance_price - ref_price) / ref_price) * 100

    return {
        "data": sorted_data,
        "closedRegions": actual_closed_regions,
        "supportPrice": support_price,
        "supportPct": support_pct,
        "resistancePrice": resistance_price,
        "resistancePct": resistance_pct,
        "maxR": max_r,
        "bestLag": best_lag,
        "correlationText": correlation_text,
        "correlationStrength": correlation_strength
    }

@app.get("/health")
async def health():
    try:
        async with get_db_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.websocket("/stats/stream")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection open and handle client disconnects
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
