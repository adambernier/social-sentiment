"""Opt-in end-to-end check: capture N published messages from a real broker.

Skipped unless ``SHADOW_CAPTURE_BROKER_URL`` names an isolated broker, e.g.::

    SHADOW_CAPTURE_BROKER_URL=amqp://guest:guest@127.0.0.1:5673/ \\
        pytest -q tests/test_capture_shadow_queue_integration.py

The queue is unique per run and deleted afterwards; never point this at a
production broker.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import aio_pika
import pytest

from scripts.capture_shadow_queue import main as capture_main

BROKER_URL = os.getenv("SHADOW_CAPTURE_BROKER_URL")

pytestmark = pytest.mark.skipif(
    not BROKER_URL,
    reason="set SHADOW_CAPTURE_BROKER_URL to an isolated broker to run",
)


async def _publish(url: str, queue: str, bodies: list[bytes]) -> None:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue, durable=True)
        for body in bodies:
            await channel.default_exchange.publish(
                aio_pika.Message(body=body), routing_key=declared.name
            )


async def _drop_queue(url: str, queue: str) -> None:
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        await channel.queue_delete(queue)


def test_capture_writes_exactly_the_published_messages(tmp_path):
    queue = f"shadow.capture.e2e.{uuid.uuid4().hex[:8]}"
    bodies = [
        json.dumps({"id": f"e2e-{index}", "platform": "bluesky"}).encode()
        for index in range(3)
    ]
    output = tmp_path / "capture.jsonl"
    try:
        asyncio.run(_publish(BROKER_URL, queue, bodies))
        exit_code = capture_main(
            [
                "--queue",
                queue,
                "--count",
                "3",
                "--output",
                str(output),
                "--rabbit-url",
                BROKER_URL,
                "--timeout",
                "30",
            ]
        )
        assert exit_code == 0
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        assert records == [json.loads(body) for body in bodies]
        assert not output.with_name(output.name + ".partial").exists()
    finally:
        asyncio.run(_drop_queue(BROKER_URL, queue))


def test_capture_timeout_keeps_a_partial_and_exits_non_zero(tmp_path):
    queue = f"shadow.capture.e2e.{uuid.uuid4().hex[:8]}"
    output = tmp_path / "capture.jsonl"
    try:
        exit_code = capture_main(
            [
                "--queue",
                queue,
                "--count",
                "1",
                "--output",
                str(output),
                "--rabbit-url",
                BROKER_URL,
                "--timeout",
                "1",
            ]
        )
    except SystemExit as error:
        assert "partial capture kept" in str(error)
    else:  # pragma: no cover - only reached if the timeout failed to fire
        pytest.fail(f"capture should have timed out, returned {exit_code}")
    partial = tmp_path / "capture.jsonl.partial"
    assert partial.exists()
    assert not output.exists()
    asyncio.run(_drop_queue(BROKER_URL, queue))
