import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, Query
from pydantic import BaseModel

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import DATABASE_DSN
from shared.symbols import primary_futures_map


# Global pool instance
db_pool: Optional[ConnectionPool] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    print("Initializing database connection pool...")
    db_pool = ConnectionPool(
        DATABASE_DSN,
        min_size=5,
        max_size=20,
        kwargs={"row_factory": psycopg.rows.dict_row}
    )
    yield
    print("Closing database connection pool...")
    db_pool.close()

app = FastAPI(title="Social Sentiment API", lifespan=lifespan)

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
    query = "SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at FROM posts"
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

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

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

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

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

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

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

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

@app.get("/stats/market/latest", response_model=Optional[MarketQuote])
async def get_latest_market_quote(symbol: str):
    query = """
        SELECT symbol, timestamp, price, volume, market_session 
        FROM stock_quotes 
        WHERE symbol = %s 
        ORDER BY timestamp DESC LIMIT 1
    """
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [symbol])
            return cur.fetchone()

@app.get("/stats/market/delta", response_model=Optional[MarketDelta])
async def get_market_delta(
    symbol: str,
    since: datetime = Query(...)
):
    """
    Calculate pct_change for a symbol since a specific reference timestamp.
    Useful for 'normalization' of futures vs. last cash close.
    """
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # 1. Get latest price
            cur.execute(
                "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )
            latest = cur.fetchone()
            if not latest:
                return None
            
            latest_price = latest['price']

            # 2. Get reference price (the one closest to 'since' but not after it)
            cur.execute(
                "SELECT price FROM stock_quotes WHERE symbol = %s AND timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
                [symbol, since]
            )
            ref = cur.fetchone()
            
            # Fallback: if no quote exists before 'since', get the first one available
            if not ref:
                cur.execute(
                    "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp ASC LIMIT 1",
                    [symbol]
                )
                ref = cur.fetchone()

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
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [symbol])
            return cur.fetchone()

@app.get("/stats/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    symbol: str,
    hours: int = Query(24, gt=0),
    since: datetime = Query(...),
    platform: Optional[str] = None
):
    futures_map = primary_futures_map()
    primary_future_symbol = futures_map.get(symbol)
    vix_symbol = "VX=F"
    
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # Helper to run query and fetch all rows
            def run_query(q, p):
                cur.execute(q, p)
                return cur.fetchall()

            # Helper to run query and fetch one row
            def run_query_one(q, p):
                cur.execute(q, p)
                return cur.fetchone()

            # Time threshold for hourly stats and graphs
            cutoff = datetime.now() - timedelta(hours=hours)

            # 1. Fetch posts matching symbol and optional platform, ordered by timestamp
            posts_query = (
                "SELECT id, symbol, platform, text, timestamp, sentiment, scores, topic_id, topic_label, scored_at "
                "FROM posts "
                "WHERE symbol = %s AND timestamp >= %s"
            )
            posts_params = [symbol, cutoff]
            if platform:
                posts_query += " AND platform = %s"
                posts_params.append(platform)
            posts_query += " ORDER BY timestamp DESC LIMIT 1000"
            posts = run_query(posts_query, posts_params)

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
            sentiment_stats = run_query(sent_query, sent_params)

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
            topic_stats = run_query(topic_query, topic_params)

            # 4. Fetch market data (quotes)
            market_query = (
                "SELECT symbol, timestamp, price, volume, market_session "
                "FROM stock_quotes "
                "WHERE symbol = %s AND timestamp > %s "
                "ORDER BY timestamp ASC"
            )
            market_data = run_query(market_query, [symbol, cutoff])

            # 5. Fetch latest quote for target
            latest_quote = run_query_one(
                "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [symbol]
            )

            # 6. Fetch target metrics
            metrics_data = run_query_one(
                "SELECT symbol, pe_ratio, beta, avg_return_1y, inflation_adj_return_1y, "
                "       pe_relative_sector, beta_relative_sector, return_relative_sector, updated_at "
                "FROM stock_metrics WHERE symbol = %s",
                [symbol]
            )

            # Delta helper
            def get_delta_data(sym, ref_time):
                cur.execute(
                    "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                    [sym]
                )
                latest = cur.fetchone()
                if not latest:
                    return None
                latest_price = latest['price']

                cur.execute(
                    "SELECT price FROM stock_quotes WHERE symbol = %s AND timestamp <= %s ORDER BY timestamp DESC LIMIT 1",
                    [sym, ref_time]
                )
                ref = cur.fetchone()
                if not ref:
                    cur.execute(
                        "SELECT price FROM stock_quotes WHERE symbol = %s ORDER BY timestamp ASC LIMIT 1",
                        [sym]
                    )
                    ref = cur.fetchone()

                if not ref:
                    return None
                ref_price = ref['price']
                
                abs_change = latest_price - ref_price
                pct_change = (abs_change / ref_price * 100) if ref_price != 0 else 0.0
                return {
                    "symbol": sym,
                    "reference_price": ref_price,
                    "latest_price": latest_price,
                    "pct_change": pct_change,
                    "abs_change": abs_change
                }

            primary_delta = get_delta_data(symbol, since)

            # 7. Fetch primary future details if available
            p_future_quote = None
            p_future_delta = None
            p_future_market_data = []
            if primary_future_symbol:
                p_future_quote = run_query_one(
                    "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                    [primary_future_symbol]
                )
                p_future_delta = get_delta_data(primary_future_symbol, since)
                p_future_market_data = run_query(market_query, [primary_future_symbol, cutoff])

            # 8. Fetch VIX details
            vix_quote = run_query_one(
                "SELECT symbol, timestamp, price, volume, market_session FROM stock_quotes WHERE symbol = %s ORDER BY timestamp DESC LIMIT 1",
                [vix_symbol]
            )
            vix_delta = get_delta_data(vix_symbol, since)

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

@app.get("/health")
async def health():
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
