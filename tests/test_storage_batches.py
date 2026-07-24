import asyncio
import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

import psycopg
import pytest

from shared.messaging import RequeueError
from shared.schemas import ScoredPost


storage_main = importlib.import_module("storage-service.main")


def _post(post_id: str) -> ScoredPost:
    return ScoredPost(
        id=post_id,
        symbol="NVDA",
        platform="reddit",
        text=f"post {post_id}",
        timestamp=datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        sentiment="positive",
        scores={"positive": 0.9, "neutral": 0.1, "negative": 0.0},
        engagement=3,
    )


def _message(post: ScoredPost):
    message = MagicMock()
    message.body = post.model_dump_json().encode()
    message.headers = {}
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


@pytest.mark.asyncio
async def test_storage_batch_acks_only_after_successful_batch_insert():
    posts = [_post("one"), _post("two")]
    messages = [_message(post) for post in posts]
    db = MagicMock()
    db.insert_scored_batch_async = AsyncMock(return_value=2)
    channel = _channel()

    result = await storage_main.persist_scored_batch(
        posts,
        messages,
        db,
        channel,
    )

    db.insert_scored_batch_async.assert_awaited_once_with(posts)
    for message in messages:
        message.ack.assert_awaited_once_with()
    channel.default_exchange.publish.assert_not_awaited()
    assert result.stored_messages == 2
    assert result.affected_rows == 2
    assert result.dead_lettered_messages == 0


@pytest.mark.asyncio
async def test_storage_batch_isolates_permanent_failure_and_dead_letters_only_bad_post():
    posts = [_post("good-one"), _post("bad"), _post("good-two")]
    messages = [_message(post) for post in posts]
    db = MagicMock()
    batch_error = psycopg.errors.CheckViolation("batch contains bad post")
    post_error = psycopg.errors.CheckViolation("bad post")
    db.insert_scored_batch_async = AsyncMock(
        side_effect=[batch_error, 1, post_error, 1]
    )
    channel = _channel()

    result = await storage_main.persist_scored_batch(
        posts,
        messages,
        db,
        channel,
    )

    assert db.insert_scored_batch_async.await_args_list == [
        call(posts),
        call([posts[0]]),
        call([posts[1]]),
        call([posts[2]]),
    ]
    for message in messages:
        message.ack.assert_awaited_once_with()
    channel.default_exchange.publish.assert_awaited_once()
    dead_letter = channel.default_exchange.publish.call_args.args[0]
    assert dead_letter.body == messages[1].body
    assert channel.default_exchange.publish.call_args.kwargs == {
        "routing_key": "scored-posts.dead-letter"
    }
    assert result.stored_messages == 2
    assert result.affected_rows == 2
    assert result.dead_lettered_messages == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            psycopg.OperationalError("database unavailable"),
            id="transient-database-error",
        ),
        pytest.param(RuntimeError("unexpected failure"), id="unexpected-error"),
    ],
)
async def test_storage_batch_does_not_isolate_non_data_failures(error):
    posts = [_post("one"), _post("two")]
    messages = [_message(post) for post in posts]
    db = MagicMock()
    db.insert_scored_batch_async = AsyncMock(side_effect=error)
    channel = _channel()

    with pytest.raises(type(error), match=str(error)):
        await storage_main.persist_scored_batch(
            posts,
            messages,
            db,
            channel,
        )

    db.insert_scored_batch_async.assert_awaited_once_with(posts)
    for message in messages:
        message.ack.assert_not_awaited()
        message.nack.assert_not_awaited()
    channel.default_exchange.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_batch_leaves_poison_message_unacked_if_dead_letter_publish_fails():
    post = _post("bad")
    message = _message(post)
    db = MagicMock()
    db.insert_scored_batch_async = AsyncMock(
        side_effect=[
            psycopg.errors.CheckViolation("bad batch"),
            psycopg.errors.CheckViolation("bad post"),
        ]
    )
    channel = _channel(publish_side_effect=RuntimeError("publish failed"))

    with pytest.raises(RuntimeError, match="publish failed"):
        await storage_main.persist_scored_batch(
            [post],
            [message],
            db,
            channel,
        )

    message.ack.assert_not_awaited()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_worker_requeues_whole_batch_after_transient_failure(
    monkeypatch,
):
    posts = [_post("one"), _post("two")]
    messages = [_message(post) for post in posts]
    requeued = asyncio.Event()
    nack_count = 0

    async def record_nack(*, requeue):
        nonlocal nack_count
        assert requeue is True
        nack_count += 1
        if nack_count == len(messages):
            requeued.set()

    for message in messages:
        message.nack.side_effect = record_nack

    db = MagicMock()
    db.insert_scored_batch_async = AsyncMock(
        side_effect=psycopg.OperationalError("database unavailable")
    )
    queue = asyncio.Queue()
    for post, message in zip(posts, messages):
        await queue.put((message, post))

    monkeypatch.setattr(storage_main, "BATCH_SIZE", len(posts))
    worker = asyncio.create_task(
        storage_main.db_writer_worker(queue, db, _channel())
    )
    try:
        await asyncio.wait_for(requeued.wait(), timeout=1)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    db.insert_scored_batch_async.assert_awaited_once_with(posts)
    for message in messages:
        message.nack.assert_awaited_once_with(requeue=True)
        message.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_worker_surfaces_requeue_failure_after_attempting_all_messages(
    monkeypatch,
):
    posts = [_post("one"), _post("two")]
    messages = [_message(post) for post in posts]
    messages[0].nack.side_effect = RuntimeError("channel closed")
    db = MagicMock()
    db.insert_scored_batch_async = AsyncMock(
        side_effect=psycopg.OperationalError("database unavailable")
    )
    queue = asyncio.Queue()
    for post, message in zip(posts, messages):
        await queue.put((message, post))

    monkeypatch.setattr(storage_main, "BATCH_SIZE", len(posts))

    with pytest.raises(RequeueError):
        await asyncio.wait_for(
            storage_main.db_writer_worker(queue, db, _channel()),
            timeout=1,
        )

    for message in messages:
        message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_storage_worker_requeues_partial_batch_and_propagates_cancellation(
    monkeypatch,
):
    post = _post("one")
    message = _message(post)
    queue = asyncio.Queue()
    await queue.put((message, post))
    monkeypatch.setattr(storage_main, "BATCH_SIZE", 2)
    monkeypatch.setattr(storage_main, "BATCH_TIMEOUT", 10)

    worker = asyncio.create_task(
        storage_main.db_writer_worker(queue, MagicMock(), _channel())
    )
    while not queue.empty():
        await asyncio.sleep(0)
    worker.cancel()

    with pytest.raises(asyncio.CancelledError):
        await worker

    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_storage_session_requeues_buffer_and_surfaces_worker_failure(
    monkeypatch,
):
    async def fail_supervisor(*_tasks):
        raise RuntimeError("database worker crashed")

    monkeypatch.setattr(
        storage_main,
        "supervise_long_running",
        fail_supervisor,
    )
    post = _post("one")
    message = _message(post)
    queue = asyncio.Queue()
    await queue.put((message, post))

    with pytest.raises(RuntimeError, match="database worker crashed"):
        await storage_main.run_consumer_session(
            MagicMock(),
            queue,
            MagicMock(),
            _channel(),
        )

    assert queue.empty()
    message.nack.assert_awaited_once_with(requeue=True)
