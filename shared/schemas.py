import math
import os
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pipeline_git_commit() -> str:
    return os.getenv("PIPELINE_GIT_COMMIT", "unknown")


class RawPost(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime
    engagement: int = 1
    ingested_at: datetime = Field(default_factory=utc_now)
    engagement_observed_at: datetime = Field(default_factory=utc_now)
    source_schema_version: int = 1
    pipeline_git_commit: str = Field(default_factory=pipeline_git_commit)


class CleanPost(BaseModel):
    id: str
    symbol: str
    platform: str
    text: str
    timestamp: datetime
    topic_id: int | None = None
    topic_label: str | None = None
    engagement: int = 1
    ingested_at: datetime = Field(default_factory=utc_now)
    engagement_observed_at: datetime = Field(default_factory=utc_now)
    source_schema_version: int = 1
    pipeline_git_commit: str = Field(default_factory=pipeline_git_commit)
    cleaned_at: datetime = Field(default_factory=utc_now)
    topic_scored_at: datetime = Field(default_factory=utc_now)
    topic_model_version: str = "legacy"
    topic_model_hash: str = "legacy"


class ScoredPost(CleanPost):
    sentiment: Literal["positive", "neutral", "negative"]
    scores: dict[str, float]
    sentiment_scored_at: datetime = Field(default_factory=utc_now)
    sentiment_model_version: str = "legacy"
    sentiment_model_hash: str = "legacy"

    @model_validator(mode="after")
    def validate_probability_scores(self):
        required = {"positive", "neutral", "negative"}
        if required - self.scores.keys():
            raise ValueError("scores must include positive, neutral, and negative")
        probabilities = [self.scores[label] for label in sorted(required)]
        if any(
            not math.isfinite(probability) or not 0 <= probability <= 1
            for probability in probabilities
        ):
            raise ValueError("sentiment probabilities must be finite and in [0, 1]")
        if not math.isclose(sum(probabilities), 1.0, abs_tol=0.0001):
            raise ValueError("sentiment probabilities must sum to one")
        return self



class StockQuote(BaseModel):
    symbol: str
    timestamp: datetime
    price: float
    volume: int
    market_session: str
    provider: str = "yfinance"


class StockMetrics(BaseModel):
    symbol: str
    pe_ratio: float | None = None
    beta: float | None = None
    avg_return_1y: float | None = None
    inflation_adj_return_1y: float | None = None
    pe_relative_sector: float | None = None
    beta_relative_sector: float | None = None
    return_relative_sector: float | None = None
