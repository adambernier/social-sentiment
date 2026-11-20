"""Manually triggered Rust worker retention and observation gates."""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from .evidence import artifact_path, evidence_artifact, git_revision, host_platform
from .harness import (
    Broker,
    QualificationStack,
    QueueNames,
    Worker,
    running_worker,
)

pytestmark = [pytest.mark.worker_qualification, pytest.mark.worker_qualification_gate]


def _selected_worker() -> Worker:
    value = os.getenv("QUALIFICATION_WORKER", "preprocessing")
    if value not in {"preprocessing", "sentiment", "storage"}:
        raise ValueError(f"invalid QUALIFICATION_WORKER: {value}")
    return cast(Worker, value)


async def _wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    description: str,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.5)
    raise TimeoutError(description)


def _current_timestamp() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _evidence_record(
    worker: Worker,
    gate: str,
    service: str,
    project_name: str,
) -> dict[str, Any]:
    """Seed every gate record with the candidate it is about to exercise.

    The Compose project name is recorded because it is the only thing that
    distinguishes images built from this revision from images a reused project
    left behind: a run that names one reuses its images instead of rebuilding
    (`QUALIFICATION_PROJECT_NAME`), and that reuse is only sound while the
    service source is unchanged since they were built.
    """
    return {
        "gate": gate,
        "worker": worker,
        "runtime": "rust",
        "service": service,
        "project_name": project_name,
        "revision": git_revision(),
        "host": host_platform(),
    }


async def _retention_result(
    stack: QualificationStack,
    payload: dict[str, Any],
) -> tuple[int, int, int]:
    stack.truncate_worker_state()
    queues = QueueNames.unique("storage-rust-retention")
    async with running_worker(stack, "storage", "rust", queues) as broker:
        await broker.publish(queues.scored, payload)
        await _wait_for(
            lambda: stack.retention_snapshot()[0] == 1,
            timeout=60,
            description="rust storage did not persist retention fixture",
        )
        await _wait_for(
            lambda: stack.retention_snapshot() == (0, 1, 1),
            timeout=100,
            description="rust retention transaction did not complete",
        )
        return stack.retention_snapshot()


async def test_storage_retention_transaction_gate(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    if os.getenv("QUALIFICATION_RUN_RETENTION") != "1":
        pytest.skip("set QUALIFICATION_RUN_RETENTION=1 to run the two-minute gate")
    payload = copy.deepcopy(qualification_fixtures["storage"]["valid"])
    old_timestamp = "2026-06-01T12:00:00Z"
    for field in (
        "timestamp",
        "ingested_at",
        "engagement_observed_at",
        "cleaned_at",
        "topic_scored_at",
        "sentiment_scored_at",
    ):
        payload[field] = old_timestamp
    rust_result = await _retention_result(
        qualification_stack,
        payload,
    )
    assert rust_result == (0, 1, 1)


async def _observation_window(
    stack: QualificationStack,
    broker: Broker,
    worker: Worker,
    queues: QueueNames,
    fixture: dict[str, Any],
    *,
    service: str,
    duration: float,
    interval: float,
    probe_interval: float,
    probe_timeout: float,
) -> dict[str, Any]:
    """Hold one worker in a live window and prove it keeps consuming.

    A running container with a quiet queue does not prove the worker still
    consumes, so the window is bounded by verified probes at their own cadence.
    """
    input_queue = queues.input_for(worker)
    output_queue = queues.output_for(worker)
    dead_letter_queue = f"{input_queue}.dead-letter"
    loop = asyncio.get_running_loop()
    samples = 0
    probes = 0

    async def probe() -> None:
        """Publish one uniquely identified record and prove it was consumed."""
        nonlocal probes
        payload = copy.deepcopy(fixture)
        payload["id"] = f"{payload['id']}-observation-{probes:06d}"
        probe_id = payload["id"]
        if worker == "storage":
            # Probes carry the current time so the retention sweep cannot
            # archive them out of `posts` mid-window.
            payload["timestamp"] = _current_timestamp()
        await broker.publish(input_queue, payload)
        probes += 1
        if output_queue is None:
            await _wait_for(
                lambda: any(
                    row["id"] == probe_id for row in stack.post_snapshot()
                ),
                timeout=probe_timeout,
                description=(
                    f"observation probe {probe_id!r} was not persisted; "
                    f"{service} is not consuming"
                ),
            )
            return
        output = (await broker.receive(output_queue, timeout=probe_timeout)).json()
        assert output["id"] == probe_id, (
            f"observation probe {probe_id!r} came back as {output['id']!r}"
        )

    await probe()
    deadline = loop.time() + duration
    next_sample = loop.time()
    next_probe = loop.time() + probe_interval
    while loop.time() < deadline:
        now = loop.time()
        if now >= next_sample:
            assert stack.service_is_running(service)
            # Read exact broker counts: the management-plugin API lags the
            # broker's actual state, so a sample taken just after the worker
            # acks a probe can read a stale unacknowledged count and fail the
            # window on a message that is already gone.
            assert stack.direct_queue_state(input_queue) == (0, 0)
            assert stack.direct_queue_state(dead_letter_queue) == (0, 0), (
                "the observation window dead-lettered messages"
            )
            samples += 1
            next_sample = now + interval
        if now >= next_probe:
            await probe()
            next_probe = loop.time() + probe_interval
        await asyncio.sleep(
            max(0.0, min(next_sample, next_probe, deadline) - loop.time())
        )

    # The window closes on a verified probe rather than on a clock check: a
    # worker that stopped consuming mid-window keeps its container running and
    # its input queue empty, and would otherwise satisfy every assertion above.
    await probe()
    if worker == "storage":
        assert len(stack.post_snapshot()) == probes
        assert stack.duplicate_count() == 0
    return {
        "liveness_samples": samples,
        "verified_probes": probes,
        "sample_interval_seconds": interval,
        "probe_interval_seconds": probe_interval,
    }


async def test_rust_observation_gate(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    duration = float(os.getenv("QUALIFICATION_OBSERVE_SECONDS", "0"))
    if duration <= 0:
        pytest.skip("set QUALIFICATION_OBSERVE_SECONDS (86400 for 24h) to run")
    interval = min(
        60.0,
        max(1.0, float(os.getenv("QUALIFICATION_OBSERVE_INTERVAL", "30"))),
    )
    # The cadence shortens for development runs shorter than four of the
    # configured intervals.
    probe_interval = max(
        1.0,
        min(
            float(os.getenv("QUALIFICATION_OBSERVE_PROBE_INTERVAL", "300")),
            duration / 4,
        ),
    )
    probe_timeout = max(
        1.0,
        float(os.getenv("QUALIFICATION_OBSERVE_PROBE_TIMEOUT", "120")),
    )
    worker = _selected_worker()
    queues = QueueNames.unique(f"{worker}-observation")
    if worker == "storage":
        qualification_stack.truncate_worker_state()
    service = f"rust-{worker}"

    record = _evidence_record(
        worker,
        "observation",
        service,
        qualification_stack.project_name,
    )
    record["duration_hours"] = duration / 3600
    with evidence_artifact(
        artifact_path("observation", worker),
        record,
    ) as evidence:
        async with running_worker(
            qualification_stack,
            worker,
            "rust",
            queues,
        ) as broker:
            evidence.update(
                await _observation_window(
                    qualification_stack,
                    broker,
                    worker,
                    queues,
                    qualification_fixtures[worker]["valid"],
                    service=service,
                    duration=duration,
                    interval=interval,
                    probe_interval=probe_interval,
                    probe_timeout=probe_timeout,
                )
            )
        evidence["images"] = qualification_stack.container_image_provenance([service])
