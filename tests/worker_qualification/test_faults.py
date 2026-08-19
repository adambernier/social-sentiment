"""Destructive dependency and lifecycle cases for manual qualification."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from typing import Any

import pytest

from .harness import (
    Broker,
    QualificationStack,
    QueueNames,
    Runtime,
    Worker,
    assert_compatible_dlq_headers,
    running_worker,
)

pytestmark = [pytest.mark.worker_qualification, pytest.mark.worker_qualification_fault]


async def _wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float = 90.0,
    description: str,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.25)
    raise TimeoutError(description)


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment"])
@pytest.mark.parametrize("runtime", ["python", "rust"])
async def test_unroutable_output_retries_then_dead_letters(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
    runtime: Runtime,
) -> None:
    queues = QueueNames.unique(f"{worker}-{runtime}-unroutable")
    async with running_worker(
        qualification_stack,
        worker,
        runtime,
        queues,
    ) as broker:
        output = queues.output_for(worker)
        assert output is not None
        await broker.delete(output)
        await broker.publish(
            queues.input_for(worker),
            qualification_fixtures[worker]["valid"],
        )
        message = await broker.receive(f"{queues.input_for(worker)}.dead-letter")

    assert_compatible_dlq_headers(
        message.headers,
        input_queue=queues.input_for(worker),
        expected_attempt=1,
    )
    assert qualification_stack.queue_state(queues.input_for(worker)) == (0, 0)


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment"])
@pytest.mark.parametrize("runtime", ["python", "rust"])
async def test_publisher_rejection_retries_then_dead_letters(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
    runtime: Runtime,
) -> None:
    queues = QueueNames.unique(f"{worker}-{runtime}-publisher-reject")
    async with running_worker(
        qualification_stack,
        worker,
        runtime,
        queues,
    ) as broker:
        output = queues.output_for(worker)
        assert output is not None
        await broker.publish(output, {"blocker": True})
        qualification_stack.reject_publishes(output)
        await broker.publish(
            queues.input_for(worker),
            qualification_fixtures[worker]["valid"],
        )
        message = await broker.receive(f"{queues.input_for(worker)}.dead-letter")

    assert_compatible_dlq_headers(
        message.headers,
        input_queue=queues.input_for(worker),
        expected_attempt=1,
    )


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment", "storage"])
async def test_persistent_input_survives_rabbitmq_restart(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    queues = QueueNames.unique(f"{worker}-rabbit-restart")
    input_queue = queues.input_for(worker)
    async with Broker(qualification_stack.amqp_url) as broker:
        await broker.declare(input_queue)
        await broker.publish(input_queue, qualification_fixtures[worker]["valid"])

    qualification_stack.restart_dependency("rabbitmq")
    if worker == "storage":
        qualification_stack.truncate_worker_state()
    async with running_worker(
        qualification_stack,
        worker,
        "rust",
        queues,
    ) as broker:
        output = queues.output_for(worker)
        if output is not None:
            await broker.receive(output)
        else:
            await _wait_for(
                lambda: len(qualification_stack.post_snapshot()) == 1,
                description="storage input was not recovered after RabbitMQ restart",
            )
    qualification_stack.wait_for_queue_state(
        input_queue,
        lambda state: state == (0, 0),
        timeout=30,
    )


@pytest.mark.parametrize("worker", ["preprocessing", "sentiment", "storage"])
async def test_sigterm_requeues_in_flight_without_loss(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
    worker: Worker,
) -> None:
    queues = QueueNames.unique(f"{worker}-sigterm")
    input_queue = queues.input_for(worker)
    message_count = 200 if worker == "preprocessing" else 20
    payloads = []
    for index in range(message_count):
        payload = copy.deepcopy(qualification_fixtures[worker]["valid"])
        payload["id"] = f"qualification-sigterm-{worker}-{index}"
        payloads.append(payload)
    if worker == "storage":
        qualification_stack.truncate_worker_state()

    service = qualification_stack.start_worker(worker, "rust", queues)
    paused = False
    try:
        qualification_stack.wait_for_queue_state(
            input_queue,
            lambda _state: True,
            timeout=90,
        )
        async with Broker(qualification_stack.amqp_url) as broker:
            qualification_stack.pause_worker(worker, "rust")
            paused = True
            for payload in payloads:
                await broker.publish(input_queue, payload)
            qualification_stack.wait_for_queue_state(
                input_queue,
                lambda state: sum(state) == len(payloads) and state[1] > 0,
                timeout=30,
            )
            qualification_stack.signal_worker(worker, "rust")
            qualification_stack.unpause_worker(worker, "rust")
            paused = False
            await _wait_for(
                lambda: not qualification_stack.service_is_running(service),
                timeout=30,
                description="worker did not terminate after SIGTERM",
            )
            qualification_stack.wait_for_queue_state(
                input_queue,
                lambda state: state[1] == 0,
                timeout=30,
            )

        qualification_stack.start_worker(worker, "rust", queues)
        async with Broker(qualification_stack.amqp_url) as broker:
            output = queues.output_for(worker)
            if output is not None:
                received_ids = {
                    (await broker.receive(output, timeout=90)).json()["id"]
                    for _payload in payloads
                }
                assert received_ids == {payload["id"] for payload in payloads}
            else:
                await _wait_for(
                    lambda: len(qualification_stack.post_snapshot()) == len(payloads),
                    timeout=90,
                    description="storage lost messages across SIGTERM",
                )
    finally:
        if paused:
            qualification_stack.unpause_worker(worker, "rust")
        qualification_stack.stop_worker(worker, "rust")

    qualification_stack.wait_for_queue_state(
        input_queue,
        lambda state: state == (0, 0),
        timeout=30,
    )


async def test_storage_requeues_during_postgres_outage(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    qualification_stack.truncate_worker_state()
    queues = QueueNames.unique("storage-postgres-outage")
    payloads = []
    for index in range(3):
        payload = copy.deepcopy(qualification_fixtures["storage"]["valid"])
        payload["id"] = f"qualification-postgres-outage-{index}"
        payloads.append(payload)

    qualification_stack.start_worker("storage", "rust", queues)
    try:
        qualification_stack.wait_for_queue_state(
            queues.scored,
            lambda _state: True,
            timeout=90,
        )
        qualification_stack.stop_dependency("postgres")
        async with Broker(qualification_stack.amqp_url) as broker:
            for payload in payloads:
                await broker.publish(queues.scored, payload)
            qualification_stack.wait_for_queue_state(
                queues.scored,
                lambda state: sum(state) == len(payloads),
            )
        qualification_stack.start_dependency("postgres")
        await _wait_for(
            lambda: len(qualification_stack.post_snapshot()) == len(payloads),
            timeout=120,
            description="storage did not recover after PostgreSQL outage",
        )
    finally:
        if not qualification_stack.service_is_running("postgres"):
            qualification_stack.start_dependency("postgres")
        qualification_stack.stop_worker("storage", "rust")

    qualification_stack.wait_for_queue_state(
        queues.scored,
        lambda state: state == (0, 0),
        timeout=30,
    )
