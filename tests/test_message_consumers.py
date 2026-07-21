import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import aio_pika
import pytest

from shared.messaging import PROCESSING_ATTEMPT_HEADER
from shared.schemas import CleanPost, RawPost


preprocessing_main = importlib.import_module("preprocessing-service.main")
sentiment_main = importlib.import_module("sentiment-service.main")


@pytest.fixture(autouse=True)
def run_model_calls_inline(monkeypatch):
    async def to_thread_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    # Avoid creating a real executor thread in these unit tests; model behavior
    # is mocked, and executor lifecycle is not what the delivery tests exercise.
    monkeypatch.setattr(preprocessing_main.asyncio, "to_thread", to_thread_inline)


def _message(body, *, headers=None):
    message = MagicMock()
    message.body = body
    message.headers = headers or {}
    message.content_type = "application/json"
    message.processed = False
    message.ack = AsyncMock()
    message.nack = AsyncMock()
    return message


def _channel(*, publish_side_effect=None):
    channel = MagicMock()
    channel.default_exchange.publish = AsyncMock(
        side_effect=publish_side_effect
    )
    return channel


def _raw_post():
    return RawPost(
        id="shared-id",
        symbol="NVDA",
        platform="reddit",
        text="NVDA reported strong quarterly earnings",
        timestamp=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
        engagement=3,
    )


def _clean_post():
    return CleanPost(
        **_raw_post().model_dump(),
        topic_id=1,
        topic_label="Earnings & Guidance",
    )


@pytest.mark.asyncio
async def test_preprocessing_dead_letters_malformed_payload():
    message = _message(b"not-json")
    channel = _channel()

    await preprocessing_main.process_message(
        message,
        MagicMock(),
        channel,
    )

    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "raw-posts.dead-letter"
    }
    message.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_preprocessing_retries_then_dead_letters_inference_failure():
    topic_model = MagicMock()
    topic_model.predict.side_effect = RuntimeError("inference failed")

    first_message = _message(_raw_post().model_dump_json().encode())
    first_channel = _channel()
    await preprocessing_main.process_message(
        first_message,
        topic_model,
        first_channel,
    )
    retry = first_channel.default_exchange.publish.call_args.args[0]
    assert first_channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "raw-posts"
    }
    assert retry.headers[PROCESSING_ATTEMPT_HEADER] == 1
    first_message.ack.assert_awaited_once_with()

    retried_message = _message(
        _raw_post().model_dump_json().encode(),
        headers={PROCESSING_ATTEMPT_HEADER: 1},
    )
    retry_channel = _channel()
    await preprocessing_main.process_message(
        retried_message,
        topic_model,
        retry_channel,
    )
    assert retry_channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "raw-posts.dead-letter"
    }
    retried_message.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_preprocessing_acks_only_after_successful_publish(monkeypatch):
    message = _message(_raw_post().model_dump_json().encode())
    channel = _channel()
    topic_model = MagicMock()
    topic_model.predict.return_value = (1, "Earnings & Guidance")
    metric = MagicMock()
    monkeypatch.setattr(preprocessing_main, "MESSAGES_PROCESSED_TOTAL", metric)

    await preprocessing_main.process_message(message, topic_model, channel)

    published = channel.default_exchange.publish.call_args.args[0]
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "clean-posts"
    }
    assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    message.ack.assert_awaited_once_with()
    metric.labels.assert_called_once_with(service="preprocessing")


@pytest.mark.asyncio
async def test_sentiment_retries_failed_publish_and_acks_retry_copy(
    monkeypatch,
):
    message = _message(_clean_post().model_dump_json().encode())
    channel = _channel(
        publish_side_effect=[RuntimeError("publish failed"), None]
    )
    model = MagicMock()
    model.predict_batch.return_value = [
        (
            "positive",
            {"positive": 0.9, "neutral": 0.1, "negative": 0.0},
        )
    ]
    metric = MagicMock()
    monkeypatch.setattr(sentiment_main, "MESSAGES_PROCESSED_TOTAL", metric)

    await sentiment_main.process_batch(
        [_clean_post()],
        [message],
        model,
        channel,
    )

    assert channel.default_exchange.publish.await_count == 2
    retry_call = channel.default_exchange.publish.call_args_list[1]
    assert retry_call.kwargs == {"routing_key": "clean-posts"}
    assert retry_call.args[0].headers[PROCESSING_ATTEMPT_HEADER] == 1
    message.ack.assert_awaited_once_with()
    metric.labels.assert_not_called()


@pytest.mark.asyncio
async def test_sentiment_acks_and_counts_successful_publish(monkeypatch):
    message = _message(_clean_post().model_dump_json().encode())
    channel = _channel()
    model = MagicMock()
    model.predict_batch.return_value = [
        (
            "positive",
            {"positive": 0.9, "neutral": 0.1, "negative": 0.0},
        )
    ]
    metric = MagicMock()
    monkeypatch.setattr(sentiment_main, "MESSAGES_PROCESSED_TOTAL", metric)

    await sentiment_main.process_batch(
        [_clean_post()],
        [message],
        model,
        channel,
    )

    published = channel.default_exchange.publish.call_args.args[0]
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "scored-posts"
    }
    assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    message.ack.assert_awaited_once_with()
    metric.labels.assert_called_once_with(service="sentiment")
    metric.labels.return_value.inc.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_sentiment_retries_all_inputs_when_result_count_is_wrong(
    monkeypatch,
):
    message = _message(_clean_post().model_dump_json().encode())
    channel = _channel()
    model = MagicMock()
    model.predict_batch.return_value = []
    metric = MagicMock()
    monkeypatch.setattr(sentiment_main, "MESSAGES_PROCESSED_TOTAL", metric)

    await sentiment_main.process_batch(
        [_clean_post()],
        [message],
        model,
        channel,
    )

    retry = channel.default_exchange.publish.call_args.args[0]
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "clean-posts"
    }
    assert retry.headers[PROCESSING_ATTEMPT_HEADER] == 1
    message.ack.assert_awaited_once_with()
    metric.labels.assert_not_called()


@pytest.mark.asyncio
async def test_sentiment_dead_letters_invalid_model_output(monkeypatch):
    message = _message(_clean_post().model_dump_json().encode())
    channel = _channel()
    model = MagicMock()
    model.predict_batch.return_value = [
        ("positive", {"positive": "not-a-float"})
    ]
    metric = MagicMock()
    monkeypatch.setattr(sentiment_main, "MESSAGES_PROCESSED_TOTAL", metric)

    await sentiment_main.process_batch(
        [_clean_post()],
        [message],
        model,
        channel,
    )

    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "clean-posts.dead-letter"
    }
    message.ack.assert_awaited_once_with()
    metric.labels.assert_not_called()
