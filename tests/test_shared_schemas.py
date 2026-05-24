from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from shared.schemas import RawPost, CleanPost, ScoredPost, StockQuote, StockMetrics

def test_raw_post_validation():
    # Valid RawPost
    post_data = {
        "id": "123",
        "symbol": "AAPL",
        "platform": "reddit",
        "text": "AAPL to the moon!",
        "timestamp": "2026-05-24T12:00:00Z",
        "engagement": 5
    }
    post = RawPost.model_validate(post_data)
    assert post.id == "123"
    assert post.engagement == 5
    assert isinstance(post.timestamp, datetime)

    # Defaults to engagement=1
    post_data_no_eng = post_data.copy()
    del post_data_no_eng["engagement"]
    post_no_eng = RawPost.model_validate(post_data_no_eng)
    assert post_no_eng.engagement == 1

    # Missing required fields
    with pytest.raises(ValidationError):
        RawPost.model_validate({"id": "123"})

def test_clean_post_validation():
    post_data = {
        "id": "123",
        "symbol": "AAPL",
        "platform": "reddit",
        "text": "AAPL to the moon!",
        "timestamp": "2026-05-24T12:00:00Z",
        "topic_id": 1,
        "topic_label": "financial earnings",
        "engagement": 10
    }
    post = CleanPost.model_validate(post_data)
    assert post.topic_label == "financial earnings"

def test_scored_post_validation():
    post_data = {
        "id": "123",
        "symbol": "AAPL",
        "platform": "reddit",
        "text": "AAPL to the moon!",
        "timestamp": "2026-05-24T12:00:00Z",
        "topic_id": 1,
        "topic_label": "financial earnings",
        "engagement": 10,
        "sentiment": "positive",
        "scores": {"positive": 0.8, "negative": 0.1, "neutral": 0.1}
    }
    post = ScoredPost.model_validate(post_data)
    assert post.sentiment == "positive"
    assert post.scores["positive"] == 0.8
