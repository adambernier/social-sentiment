"""Shared RabbitMQ delivery policy for pipeline consumers.

The existing queues are durable and carry persistent messages, so changing
their declaration arguments in place would make RabbitMQ reject deployments
where those queues already exist. Instead, each input queue gets a separate
durable ``.dead-letter`` queue on the default exchange. Consumers explicitly
route malformed or repeatedly failing payloads there before acknowledging the
original message.
"""

import asyncio
import logging
from collections.abc import Iterable

import aio_pika


PROCESSING_ATTEMPT_HEADER = "x-processing-attempt"
logger = logging.getLogger("messaging")


class RequeueError(RuntimeError):
    """One or more RabbitMQ deliveries could not be returned to their queue."""

    def __init__(
        self,
        failures: list[tuple[aio_pika.IncomingMessage, Exception]],
        attempted_count: int,
    ):
        self.failures = tuple(failures)
        self.failed_count = len(failures)
        self.attempted_count = attempted_count
        super().__init__(
            f"failed to requeue {self.failed_count} of "
            f"{self.attempted_count} unprocessed RabbitMQ messages"
        )


def dead_letter_queue_name(input_queue: str) -> str:
    return f"{input_queue}.dead-letter"


async def declare_dead_letter_queue(
    channel: aio_pika.Channel,
    input_queue: str,
) -> None:
    await channel.declare_queue(
        dead_letter_queue_name(input_queue),
        durable=True,
    )


async def dead_letter_message(
    message: aio_pika.IncomingMessage,
    channel: aio_pika.Channel,
    input_queue: str,
    error: BaseException,
) -> None:
    """Persist a failed payload for inspection/replay, then acknowledge it.

    Publishing happens first. If RabbitMQ cannot confirm the dead-letter copy,
    the exception propagates and the original remains unacknowledged so the
    broker can redeliver it after the channel reconnects.
    """
    headers = dict(message.headers or {})
    headers.update(
        {
            "x-original-queue": input_queue,
            "x-error-type": type(error).__name__,
            "x-error": str(error)[:500],
        }
    )
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            content_type=message.content_type or "application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers,
        ),
        routing_key=dead_letter_queue_name(input_queue),
    )
    await message.ack()


async def retry_or_dead_letter(
    message: aio_pika.IncomingMessage,
    channel: aio_pika.Channel,
    input_queue: str,
    error: BaseException,
) -> str:
    """Retry one processing failure; dead-letter a payload that fails again.

    RabbitMQ's ``redelivered`` flag is also set after a normal shutdown requeue,
    so it cannot distinguish a failed attempt from lifecycle churn. A confirmed,
    persistent copy with an explicit attempt header provides that distinction.
    The original is acknowledged only after the broker accepts the retry copy.
    """
    headers = dict(message.headers or {})
    try:
        processing_attempt = int(headers.get(PROCESSING_ATTEMPT_HEADER, 0))
    except (TypeError, ValueError):
        processing_attempt = 0

    if processing_attempt >= 1:
        await dead_letter_message(message, channel, input_queue, error)
        return "dead-lettered"

    headers.update(
        {
            PROCESSING_ATTEMPT_HEADER: processing_attempt + 1,
            "x-last-error-type": type(error).__name__,
            "x-last-error": str(error)[:500],
        }
    )
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            content_type=message.content_type or "application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers,
        ),
        routing_key=input_queue,
    )
    await message.ack()
    return "requeued"


async def requeue_unprocessed(message: aio_pika.IncomingMessage) -> None:
    """Return in-flight work during shutdown/channel errors without penalizing it."""
    if not message.processed:
        await message.nack(requeue=True)


async def requeue_unprocessed_messages(
    messages: Iterable[aio_pika.IncomingMessage],
) -> int:
    """Attempt every unprocessed delivery and report any failed dispositions.

    Continuing after an individual ``nack`` failure prevents one broken
    delivery object from hiding the disposition of healthy neighbors. Raising
    after all attempts lets the owning consumer tear down its channel, at which
    point RabbitMQ returns any deliveries that remain unacknowledged.
    """
    attempted_count = 0
    requeued_count = 0
    failures: list[tuple[aio_pika.IncomingMessage, Exception]] = []

    for message in messages:
        if message.processed:
            continue

        attempted_count += 1
        try:
            await requeue_unprocessed(message)
        except Exception as error:
            failures.append((message, error))
            logger.error(
                "Failed to requeue RabbitMQ delivery %s: %s",
                getattr(message, "delivery_tag", "unknown"),
                error,
                exc_info=True,
            )
        else:
            requeued_count += 1

    if failures:
        raise RequeueError(failures, attempted_count)

    return requeued_count


async def requeue_buffered_messages(
    queue: asyncio.Queue[tuple[aio_pika.IncomingMessage, object]],
) -> int:
    """Drain a local consumer buffer and return every unprocessed delivery.

    Items are removed from the process-local queue before dispositions are
    attempted. If a disposition fails and the caller reconnects, stale message
    objects from the closed channel therefore cannot leak into the next
    RabbitMQ session.
    """
    messages: list[aio_pika.IncomingMessage] = []
    while True:
        try:
            message, _payload = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        messages.append(message)

    return await requeue_unprocessed_messages(messages)
