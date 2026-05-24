import sys
import asyncio
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
        try:
            self.active_connections.remove(websocket)
        except ValueError:
            pass

    async def broadcast(self, message: str):
        dead: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)

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
    rawPrice: Optional[float] = None
    buySignal: Optional[bool] = None
    buyScore: Optional[float] = None


class ClosedRegion(BaseModel):
    start: str
    end: str


class OpportunityResponse(BaseModel):
    score: float
    classification: str
    color: str
    strategy: str
    description: str
    checklist: list[str]


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
    opportunity: Optional[OpportunityResponse] = None


def compute_opportunity(
    price: Optional[float],
    support: float,
    resistance: float,
    sentiment: float,
    sentiment_sma: float,
    vix_price: Optional[float] = None,
    pe_relative: Optional[float] = None,
    prev_prices: Optional[list[float]] = None,
    prev_sentiments: Optional[list[float]] = None
) -> dict:
    score = 0.0
    checklist = []
    
    # 1. Price near support
    if price and support and price > 0:
        dist_to_support = (price - support) / price
        if dist_to_support <= 0.025: # within 2.5% of support
            score += 3.0
            checklist.append("Price near support level")
        elif dist_to_support <= 0.05: # within 5% of support
            score += 1.5
            checklist.append("Price approaching support level")
            
    # 2. Sentiment momentum (crossover)
    if sentiment is not None and sentiment_sma is not None:
        if sentiment > sentiment_sma:
            if sentiment_sma > 0:
                score += 3.0
                checklist.append("Bullish sentiment crossover (above positive SMA)")
            else:
                score += 1.5
                checklist.append("Bullish sentiment crossover (above negative SMA)")
                
    # 3. Sentiment-price divergence (price down, sentiment up in recent trailing window)
    if prev_prices and prev_sentiments and len(prev_prices) >= 3 and len(prev_sentiments) >= 3:
        price_trend = prev_prices[-1] - prev_prices[0]
        sentiment_trend = prev_sentiments[-1] - prev_sentiments[0]
        if price_trend < 0 and sentiment_trend > 0.1:
            score += 2.0
            checklist.append("Bullish sentiment divergence (price down, sentiment rising)")
            
    # 4. Valuation vs sector benchmark
    if pe_relative is not None:
        if pe_relative < 0:
            score += 2.0
            checklist.append("Undervalued relative to sector benchmarks")
        elif pe_relative == 0:
            score += 1.0
            checklist.append("Fairly valued relative to sector benchmarks")
            
    opportunity_pct = min(100.0, (score / 10.0) * 100.0)
    
    # Classification
    if opportunity_pct >= 75.0:
        classification = "STRONG BUY"
        color = "emerald"
    elif opportunity_pct >= 50.0:
        classification = "ACCUMULATE"
        color = "teal"
    elif opportunity_pct >= 30.0:
        classification = "HOLD / NEUTRAL"
        color = "slate"
    else:
        classification = "CAUTION / OVERBOUGHT"
        color = "rose"
        
    # Strategy Recommendation
    strategy = "Hold / Sell Covered Calls"
    strategy_desc = "Neutral market stance. Capitalize on range-bound behavior using covered call writing."
    
    vix = vix_price if vix_price is not None else 15.0
    
    if classification in ("STRONG BUY", "ACCUMULATE"):
        if vix < 15.0:
            strategy = "Long Call Option (Buy Call)"
            strategy_desc = "Low volatility regime. Buy outright call options to leverage cheap premium."
        elif vix <= 22.0:
            strategy = "Long Shares / Bull Call Spread"
            strategy_desc = "Moderate volatility. Buy shares directly or use bull call debit spreads."
        else:
            strategy = "Sell Put Credit Spreads"
            strategy_desc = "High volatility regime. Sell put credit spreads to harvest rich premium."
    elif classification == "CAUTION / OVERBOUGHT":
        if vix > 22.0:
            strategy = "Sell Call Credit Spreads"
            strategy_desc = "High volatility overbought. Sell call credit spreads for fast premium decay."
        else:
            strategy = "Protect Longs / Buy Puts"
            strategy_desc = "Overbought stance. Trim shares or buy puts to hedge downside risk."
            
    return {
        "score": opportunity_pct,
        "classification": classification,
        "color": color,
        "strategy": strategy,
        "description": strategy_desc,
        "checklist": checklist
    }



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
    offset: int = Query(0, ge=0)
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
    hours: int = Query(24, gt=0, le=8760)
):
    query = """
        SELECT sentiment, COUNT(*) as count 
        FROM posts 
        WHERE timestamp > %s
    """
    params = [datetime.now(timezone.utc) - timedelta(hours=hours)]

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
    hours: int = Query(24, gt=0, le=8760)
):
    query = """
        SELECT topic_label, COUNT(*) as count 
        FROM posts 
        WHERE timestamp > %s
    """
    params = [datetime.now(timezone.utc) - timedelta(hours=hours)]

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
    hours: int = Query(24, gt=0, le=8760)
):
    query = """
        SELECT symbol, timestamp, price, volume, market_session 
        FROM stock_quotes 
        WHERE symbol = %s AND timestamp > %s
        ORDER BY timestamp ASC
    """
    params = [symbol, datetime.now(timezone.utc) - timedelta(hours=hours)]

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
    hours: int = Query(24, gt=0, le=8760),
    platform: Optional[str] = None
):
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
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

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
    hours: int = Query(24, gt=0, le=8760),
    platform: Optional[str] = None,
    topic: Optional[str] = None
):
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    futures_map = primary_futures_map()
    primary_future_symbol = futures_map.get(symbol)
    pe_relative = None
    vix_price = 15.0
    
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
            "sentimentSMA": 0.0,
            "rawPrice": None,
            "buySignal": None,
            "buyScore": None
        }

    async with get_db_conn() as conn:
        async with conn.cursor() as cur:
            # 1. Fetch market quotes for target
            # We query from 1 hour prior to cutoff to calculate hourly returns for the first hour of the window
            db_cutoff = cutoff - timedelta(hours=1)
            market_query = """
                SELECT timestamp, price, volume, market_session 
                FROM stock_quotes 
                WHERE symbol = %s AND timestamp >= %s
                ORDER BY timestamp ASC
            """
            await cur.execute(market_query, [symbol, db_cutoff])
            market_data = await cur.fetchall()
            
            # Chronological returns calculation (percentage change from previous hour)
            market_data = sorted(market_data, key=lambda x: x['timestamp'])
            for idx in range(1, len(market_data)):
                prev_q = market_data[idx - 1]
                curr_q = market_data[idx]
                curr_ts = curr_q['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                curr_ts_str = curr_ts.isoformat().replace("+00:00", "Z")
                if curr_ts_str in buckets:
                    prev_price = prev_q['price']
                    if prev_price and prev_price != 0:
                        buckets[curr_ts_str]['priceChange'] = ((curr_q['price'] - prev_price) / prev_price) * 100
                    else:
                        buckets[curr_ts_str]['priceChange'] = 0.0
                    buckets[curr_ts_str]['rawPrice'] = curr_q['price']
                    buckets[curr_ts_str]['isMarketOpen'] = True

            # 2. Fetch market quotes for primary future
            if primary_future_symbol:
                await cur.execute(market_query, [primary_future_symbol, db_cutoff])
                future_market_data = await cur.fetchall()
                future_market_data = sorted(future_market_data, key=lambda x: x['timestamp'])
                for idx in range(1, len(future_market_data)):
                    prev_q = future_market_data[idx - 1]
                    curr_q = future_market_data[idx]
                    curr_ts = curr_q['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                    curr_ts_str = curr_ts.isoformat().replace("+00:00", "Z")
                    if curr_ts_str in buckets:
                        prev_price = prev_q['price']
                        if prev_price and prev_price != 0:
                            buckets[curr_ts_str]['futureChange'] = ((curr_q['price'] - prev_price) / prev_price) * 100
                        else:
                            buckets[curr_ts_str]['futureChange'] = 0.0

            # 3. Load pre-aggregated data from hourly_sentiment_agg (cold tier).
            # This covers hours where raw posts may have been pruned.
            agg_query = """
                SELECT bucket_hour, positive_count, neutral_count, negative_count,
                       positive_weighted, negative_weighted, neutral_weighted,
                       total_weighted, sentiment_index
                FROM hourly_sentiment_agg
                WHERE symbol = %s AND bucket_hour >= %s
            """
            await cur.execute(agg_query, [symbol, cutoff])
            agg_data = await cur.fetchall()

            for a in agg_data:
                a_ts = a['bucket_hour'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                a_ts_str = a_ts.isoformat().replace("+00:00", "Z")
                if a_ts_str in buckets:
                    buckets[a_ts_str]['positive'] = a['positive_count']
                    buckets[a_ts_str]['neutral'] = a['neutral_count']
                    buckets[a_ts_str]['negative'] = a['negative_count']
                    buckets[a_ts_str]['positiveWeighted'] = a['positive_weighted']
                    buckets[a_ts_str]['negativeWeighted'] = a['negative_weighted']
                    buckets[a_ts_str]['neutralWeighted'] = a['neutral_weighted']
                    buckets[a_ts_str]['totalWeighted'] = a['total_weighted']
                    buckets[a_ts_str]['sentimentIndex'] = a['sentiment_index']

            # 4. Overlay with live posts data (hot tier).
            # For buckets that still have raw posts, this overwrites the aggregated
            # values with fresh counts computed from individual posts.
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

            # Track which buckets have live posts so we can overwrite agg data
            live_buckets: set[str] = set()
            
            for p in posts_data:
                p_ts = p['timestamp'].astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                p_ts_str = p_ts.isoformat().replace("+00:00", "Z")
                if p_ts_str in buckets:
                    # On first live post for this bucket, reset from aggregated values
                    if p_ts_str not in live_buckets:
                        live_buckets.add(p_ts_str)
                        buckets[p_ts_str]['positive'] = 0
                        buckets[p_ts_str]['neutral'] = 0
                        buckets[p_ts_str]['negative'] = 0
                        buckets[p_ts_str]['positiveWeighted'] = 0.0
                        buckets[p_ts_str]['negativeWeighted'] = 0.0
                        buckets[p_ts_str]['neutralWeighted'] = 0.0
                        buckets[p_ts_str]['totalWeighted'] = 0.0

                    engagement = p['engagement'] if p['engagement'] is not None else 1
                    weight = math.log1p(engagement)
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

            # 5. Fetch latest quote for regular session check
            await cur.execute(
                "SELECT market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )
            latest_quote = await cur.fetchone()

            # 6. Fetch valuation metrics (P/E relative to sector)
            await cur.execute(
                "SELECT pe_relative_sector FROM stock_metrics WHERE symbol = %s ORDER BY updated_at DESC LIMIT 1",
                [symbol]
            )
            metrics_row = await cur.fetchone()
            pe_relative = metrics_row['pe_relative_sector'] if metrics_row else None

            # 7. Fetch VIX quote
            await cur.execute(
                "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                ["VX=F"]
            )
            vix_row = await cur.fetchone()
            vix_price = vix_row['price'] if vix_row else 15.0

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
        latest_price = prices[-1]
        if latest_price and latest_price != 0:
            support_pct = ((support_price - latest_price) / latest_price) * 100
            resistance_pct = ((resistance_price - latest_price) / latest_price) * 100

    # 9. Compute historical buy signals
    for i in range(len(sorted_data)):
        bucket_price = sorted_data[i]['rawPrice']
        
        # Build trailing 3-hour lists for divergence check
        prev_prices = []
        prev_sentiments = []
        for k in range(max(0, i - 2), i + 1):
            if sorted_data[k]['rawPrice'] is not None:
                prev_prices.append(sorted_data[k]['rawPrice'])
            if sorted_data[k]['sentimentIndex'] is not None:
                prev_sentiments.append(sorted_data[k]['sentimentIndex'])
                
        opp = compute_opportunity(
            price=bucket_price,
            support=support_price,
            resistance=resistance_price,
            sentiment=sorted_data[i]['sentimentIndex'],
            sentiment_sma=sorted_data[i]['sentimentSMA'],
            vix_price=vix_price,
            pe_relative=pe_relative,
            prev_prices=prev_prices,
            prev_sentiments=prev_sentiments
        )
        sorted_data[i]['buyScore'] = opp['score']
        sorted_data[i]['buySignal'] = opp['score'] >= 50.0

    # 10. Compute final current opportunity
    latest_item = sorted_data[-1] if len(sorted_data) > 0 else None
    latest_prices = []
    latest_sentiments = []
    if len(sorted_data) >= 3:
        latest_prices = [d['rawPrice'] for d in sorted_data[-3:] if d['rawPrice'] is not None]
        latest_sentiments = [d['sentimentIndex'] for d in sorted_data[-3:] if d['sentimentIndex'] is not None]
        
    current_opp = compute_opportunity(
        price=latest_item['rawPrice'] if latest_item else None,
        support=support_price,
        resistance=resistance_price,
        sentiment=latest_item['sentimentIndex'] if latest_item else 0.0,
        sentiment_sma=latest_item['sentimentSMA'] if latest_item else 0.0,
        vix_price=vix_price,
        pe_relative=pe_relative,
        prev_prices=latest_prices,
        prev_sentiments=latest_sentiments
    ) if latest_item else None

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
        "correlationStrength": correlation_strength,
        "opportunity": current_opp
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
