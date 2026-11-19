"""Deterministic live-container Rust worker qualification cases for CI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from .harness import (
    QualificationStack,
    QueueNames,
    Worker,
    assert_compatible_dlq_headers,
    running_worker,
)

pytestmark = [pytest.mark.worker_qualification, pytest.mark.worker_qualification_smoke]


async def _wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float = 45.0,
    description: str,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.25)
    raise TimeoutError(description)


async def _run_output_worker(
    stack: QualificationStack,
    worker: Worker,
    payload: dict[str, Any],
) -> dict[str, Any]:
    queues = QueueNames.unique(f"{worker}-rust-valid")
    async with running_worker(stack, worker, "rust", queues) as broker:
        await broker.publish(queues.input_for(worker), payload)
        output = queues.output_for(worker)
        assert output is not None
        message = await broker.receive(output)
        stack.wait_for_queue_state(
            queues.input_for(worker),
            lambda state: state == (0, 0),
        )
        assert not message.redelivered
        return message.json()


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment"])
async def test_valid_rust_worker_produces_expected_output(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    payload = qualification_fixtures[worker]["valid"]
    rust_output = await _run_output_worker(
        qualification_stack,
        worker,
        payload,
    )
    assert rust_output["id"] == payload["id"]
    assert rust_output["symbol"] == payload["symbol"]
    if worker == "preprocessing":
        assert rust_output["topic_label"]
        assert rust_output["topic_model_hash"]
    else:
        assert rust_output["sentiment"] in {"positive", "negative", "neutral"}
        assert set(rust_output["scores"]) == {"positive", "negative", "neutral"}
        assert rust_output["sentiment_model_hash"]


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment", "storage"])
async def test_malformed_payload_reaches_compatible_dlq(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    queues = QueueNames.unique(f"{worker}-rust-malformed")
    malformed = qualification_fixtures[worker]["malformed"]
    async with running_worker(
        qualification_stack,
        worker,
        "rust",
        queues,
    ) as broker:
        await broker.publish(queues.input_for(worker), malformed)
        message = await broker.receive(f"{queues.input_for(worker)}.dead-letter")

    assert message.body == malformed.encode()
    assert_compatible_dlq_headers(
        message.headers,
        input_queue=queues.input_for(worker),
    )
    assert qualification_stack.queue_state(queues.input_for(worker)) == (0, 0)


async def test_storage_duplicate_state_is_idempotent(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    payload = qualification_fixtures["storage"]["valid"]
    qualification_stack.truncate_worker_state()
    queues = QueueNames.unique("storage-rust-duplicate")
    async with running_worker(qualification_stack, "storage", "rust", queues) as broker:
        await broker.publish(queues.scored, payload)
        await broker.publish(queues.scored, payload)
        await _wait_for(
            lambda: (
                len(qualification_stack.post_snapshot()) == 1
                and qualification_stack.duplicate_count() == 1
            ),
            timeout=60,
            description="rust storage did not persist/idempotently count duplicate",
        )
        qualification_stack.wait_for_queue_state(
            queues.scored, lambda state: state == (0, 0)
        )
        assert len(qualification_stack.post_snapshot()) == 1
        assert qualification_stack.duplicate_count() == 1
