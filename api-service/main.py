import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg
from fastapi import FastAPI, Query
from pydantic import BaseModel

# Setup path for shared imports
sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import DATABASE_DSN

app = FastAPI(title="Social Sentiment API")

class PostResponse(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime
    sentiment: str
    scores: dict[str, float]
    scored_at: datetime

class SentimentStats(BaseModel):
    sentiment: str
    count: int

class MarketQuote(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    volume: int

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

def get_db_conn():
    return psycopg.connect(DATABASE_DSN, row_factory=psycopg.rows.dict_row)

@app.get("/posts", response_model=list[PostResponse])
async def get_posts(
    symbol: Optional[str] = None,
    platform: Optional[str] = None,
    sentiment: Optional[str] = None,
    limit: int = Query(20, le=1000),
    offset: int = 0
):
    query = "SELECT id, symbol, platform, text, timestamp, sentiment, scores, scored_at FROM posts"
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

@app.get("/stats/market", response_model=list[MarketQuote])
async def get_market_stats(
    symbol: str,
    hours: int = Query(24, gt=0)
):
    query = """
        SELECT symbol, timestamp, price, volume 
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
        SELECT symbol, timestamp, price, volume 
        FROM stock_quotes 
        WHERE symbol = %s 
        ORDER BY timestamp DESC LIMIT 1
    """
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [symbol])
            return cur.fetchone()

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
