import asyncio
from unittest.mock import AsyncMock, MagicMock

import aio_pika
import pytest

from shared.messaging import (
    PROCESSING_ATTEMPT_HEADER,
    RequeueError,
    dead_letter_queue_name,
    declare_dead_letter_queue,
    requeue_buffered_messages,
    requeue_unprocessed,
    requeue_unprocessed_messages,
    retry_or_dead_letter,
)


def _message(*, headers=None, processed=False, redelivered=False):
    message = MagicMock()
    message.body = b'{"id":"post-1"}'
    message.headers = headers or {}
    message.content_type = "application/json"
    message.processed = processed
    message.redelivered = redelivered
    message.ack = AsyncMock()
    message.nack = AsyncMock()
    return message


def _channel():
    channel = MagicMock()
    channel.declare_queue = AsyncMock()
    channel.default_exchange.publish = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_declares_separate_durable_dead_letter_queue():
    channel = _channel()

    await declare_dead_letter_queue(channel, "raw-posts")

    channel.declare_queue.assert_awaited_once_with(
        "raw-posts.dead-letter",
        durable=True,
    )


@pytest.mark.asyncio
async def test_first_failure_publishes_confirmed_retry_with_attempt_header():
    message = _message(redelivered=True)
    channel = _channel()

    disposition = await retry_or_dead_letter(
        message,
        channel,
        "raw-posts",
        RuntimeError("model unavailable"),
    )

    assert disposition == "requeued"
    published_message = channel.default_exchange.publish.call_args.args[0]
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "raw-posts"
    }
    assert published_message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert published_message.headers[PROCESSING_ATTEMPT_HEADER] == 1
    assert published_message.headers["x-last-error-type"] == "RuntimeError"
    message.ack.assert_awaited_once_with()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_failure_moves_payload_to_dead_letter_queue():
    message = _message(headers={PROCESSING_ATTEMPT_HEADER: 1})
    channel = _channel()

    disposition = await retry_or_dead_letter(
        message,
        channel,
        "clean-posts",
        ValueError("invalid model output"),
    )

    assert disposition == "dead-lettered"
    published_message = channel.default_exchange.publish.call_args.args[0]
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": dead_letter_queue_name("clean-posts")
    }
    assert published_message.body == message.body
    assert published_message.headers["x-original-queue"] == "clean-posts"
    assert published_message.headers["x-error-type"] == "ValueError"
    message.ack.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_retry_publish_leaves_original_unacknowledged():
    message = _message()
    channel = _channel()
    channel.default_exchange.publish.side_effect = RuntimeError("broker down")

    with pytest.raises(RuntimeError, match="broker down"):
        await retry_or_dead_letter(
            message,
            channel,
            "raw-posts",
            RuntimeError("processing failed"),
        )

    message.ack.assert_not_awaited()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_requeues_only_unprocessed_messages():
    unprocessed = _message()
    processed = _message(processed=True)

    await requeue_unprocessed(unprocessed)
    await requeue_unprocessed(processed)

    unprocessed.nack.assert_awaited_once_with(requeue=True)
    processed.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_requeue_attempts_every_message_and_surfaces_failures(caplog):
    failed = _message()
    failed.nack.side_effect = RuntimeError("channel closed")
    processed = _message(processed=True)
    requeued = _message()

    with pytest.raises(RequeueError) as exc_info:
        await requeue_unprocessed_messages([failed, processed, requeued])

    failed.nack.assert_awaited_once_with(requeue=True)
    processed.nack.assert_not_awaited()
    requeued.nack.assert_awaited_once_with(requeue=True)
    assert exc_info.value.failed_count == 1
    assert exc_info.value.attempted_count == 2
    assert "channel closed" in caplog.text


@pytest.mark.asyncio
async def test_batch_requeue_succeeds_when_all_unprocessed_messages_are_nacked():
    messages = [_message(), _message(), _message(processed=True)]

    requeued_count = await requeue_unprocessed_messages(messages)

    assert requeued_count == 2
    messages[0].nack.assert_awaited_once_with(requeue=True)
    messages[1].nack.assert_awaited_once_with(requeue=True)
    messages[2].nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_buffer_requeue_drains_stale_deliveries_even_when_nack_fails():
    queue = asyncio.Queue()
    failed = _message()
    failed.nack.side_effect = RuntimeError("channel closed")
    requeued = _message()
    await queue.put((failed, object()))
    await queue.put((requeued, object()))

    with pytest.raises(RequeueError):
        await requeue_buffered_messages(queue)

    assert queue.empty()
    failed.nack.assert_awaited_once_with(requeue=True)
    requeued.nack.assert_awaited_once_with(requeue=True)
