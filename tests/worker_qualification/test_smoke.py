"""Deterministic live-container qualification cases suitable for CI."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from typing import Any

import pytest

from .harness import (
    QualificationStack,
    QueueNames,
    Runtime,
    Worker,
    assert_compatible_dlq_headers,
    assert_worker_payload_parity,
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
    runtime: Runtime,
    payload: dict[str, Any],
) -> dict[str, Any]:
    queues = QueueNames.unique(f"{worker}-{runtime}-valid")
    async with running_worker(stack, worker, runtime, queues) as broker:
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
async def test_valid_python_rust_payload_parity(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    payload = qualification_fixtures[worker]["valid"]
    python_output = await _run_output_worker(
        qualification_stack,
        worker,
        "python",
        payload,
    )
    rust_output = await _run_output_worker(
        qualification_stack,
        worker,
        "rust",
        payload,
    )
    assert_worker_payload_parity(worker, python_output, rust_output)


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment", "storage"])
@pytest.mark.parametrize("runtime", ["python", "rust"])
async def test_malformed_payload_reaches_compatible_dlq(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
    runtime: Runtime,
) -> None:
    queues = QueueNames.unique(f"{worker}-{runtime}-malformed")
    malformed = qualification_fixtures[worker]["malformed"]
    async with running_worker(
        qualification_stack,
        worker,
        runtime,
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


async def _storage_snapshot(
    stack: QualificationStack,
    runtime: Runtime,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    stack.truncate_worker_state()
    queues = QueueNames.unique(f"storage-{runtime}-duplicate")
    async with running_worker(stack, "storage", runtime, queues) as broker:
        await broker.publish(queues.scored, payload)
        await broker.publish(queues.scored, payload)
        await _wait_for(
            lambda: len(stack.post_snapshot()) == 1 and stack.duplicate_count() == 1,
            timeout=60,
            description=f"{runtime} storage did not persist/idempotently count duplicate",
        )
        stack.wait_for_queue_state(queues.scored, lambda state: state == (0, 0))
        return stack.post_snapshot(), stack.duplicate_count()


async def test_storage_duplicate_state_matches_python(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    payload = qualification_fixtures["storage"]["valid"]
    python_rows, python_duplicates = await _storage_snapshot(
        qualification_stack,
        "python",
        payload,
    )
    rust_rows, rust_duplicates = await _storage_snapshot(
        qualification_stack,
        "rust",
        payload,
    )
    assert rust_rows == python_rows
    assert rust_duplicates == python_duplicates == 1


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment", "storage"])
async def test_python_is_one_command_runtime_rollback(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    """Exercise the same Rust-to-Python service replacement used by rollback."""
    queues = QueueNames.unique(f"{worker}-rollback")
    payload = copy.deepcopy(qualification_fixtures[worker]["valid"])
    if worker == "storage":
        qualification_stack.truncate_worker_state()

    async with running_worker(
        qualification_stack,
        worker,
        "rust",
        queues,
    ) as broker:
        await broker.publish(queues.input_for(worker), payload)
        output = queues.output_for(worker)
        if output is not None:
            await broker.receive(output)
        else:
            await _wait_for(
                lambda: len(qualification_stack.post_snapshot()) == 1,
                description="Rust storage did not persist rollback probe",
            )
        qualification_stack.wait_for_queue_state(
            queues.input_for(worker),
            lambda state: state == (0, 0),
            stable_for=1.0,
        )

    payload["id"] = "qualification-post-after-rollback"
    async with running_worker(
        qualification_stack,
        worker,
        "python",
        queues,
    ) as broker:
        await broker.publish(queues.input_for(worker), payload)
        output = queues.output_for(worker)
        if output is not None:
            message = await broker.receive(output)
            assert message.json()["id"] == payload["id"]
        else:
            await _wait_for(
                lambda: len(qualification_stack.post_snapshot()) == 2,
                description="Python storage rollback did not persist probe",
            )
        qualification_stack.wait_for_queue_state(
            queues.input_for(worker),
            lambda state: state == (0, 0),
            stable_for=1.0,
        )

    assert qualification_stack.queue_state(queues.input_for(worker)) == (0, 0)
