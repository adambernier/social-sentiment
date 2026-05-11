from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class RawPost(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime


class CleanPost(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime


class ScoredPost(CleanPost):
    sentiment: str
    scores: dict[str, float]


class StockQuote(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    volume: int


class StockMetrics(BaseModel):
    symbol: str
    pe_ratio: Optional[float] = None
    beta: Optional[float] = None
    avg_return_1y: Optional[float] = None
    inflation_adj_return_1y: Optional[float] = None
    pe_relative_sector: Optional[float] = None
    beta_relative_sector: Optional[float] = None
    return_relative_sector: Optional[float] = None
