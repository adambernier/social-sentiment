"""Manually triggered replay, retention, and observation promotion gates."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from .harness import (
    QualificationStack,
    QueueNames,
    Runtime,
    Worker,
    assert_worker_payload_parity,
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


def _replay_payloads(
    fixture: dict[str, Any],
    count: int,
) -> list[dict[str, Any]]:
    payloads = []
    for index in range(count):
        payload = copy.deepcopy(fixture)
        payload["id"] = f"qualification-replay-{index:06d}"
        payloads.append(payload)
    return payloads


async def _output_replay(
    stack: QualificationStack,
    worker: Worker,
    runtime: Runtime,
    payloads: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    queues = QueueNames.unique(f"{worker}-{runtime}-replay")
    outputs: dict[str, dict[str, Any]] = {}
    async with running_worker(stack, worker, runtime, queues) as broker:
        for payload in payloads:
            await broker.publish(queues.input_for(worker), payload)
        output_queue = queues.output_for(worker)
        assert output_queue is not None
        for _payload in payloads:
            output = (await broker.receive(output_queue, timeout=300)).json()
            outputs[output["id"]] = output
        stack.wait_for_queue_state(
            queues.input_for(worker),
            lambda state: state == (0, 0),
            timeout=120,
        )
    assert len(outputs) == len(payloads)
    return outputs


async def _storage_replay(
    stack: QualificationStack,
    runtime: Runtime,
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    stack.truncate_worker_state()
    queues = QueueNames.unique(f"storage-{runtime}-replay")
    duplicate_payloads = payloads[::10]
    async with running_worker(stack, "storage", runtime, queues) as broker:
        for payload in [*payloads, *duplicate_payloads]:
            await broker.publish(queues.scored, payload)
        await _wait_for(
            lambda: (
                len(stack.post_snapshot()) == len(payloads)
                and stack.duplicate_count() == len(duplicate_payloads)
            ),
            timeout=300,
            description=f"{runtime} storage replay did not converge",
        )
        stack.wait_for_queue_state(
            queues.scored,
            lambda state: state == (0, 0),
            timeout=120,
        )
        return stack.post_snapshot(), stack.duplicate_count()


async def test_recorded_replay_gate(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    count = int(os.getenv("QUALIFICATION_REPLAY_COUNT", "0"))
    if count <= 0:
        pytest.skip("set QUALIFICATION_REPLAY_COUNT (normally 1000) to run")
    worker = _selected_worker()
    payloads = _replay_payloads(
        qualification_fixtures[worker]["valid"],
        count,
    )

    if worker == "storage":
        python_state = await _storage_replay(
            qualification_stack,
            "python",
            payloads,
        )
        rust_state = await _storage_replay(
            qualification_stack,
            "rust",
            payloads,
        )
        assert rust_state == python_state
        return

    python_outputs = await _output_replay(
        qualification_stack,
        worker,
        "python",
        payloads,
    )
    rust_outputs = await _output_replay(
        qualification_stack,
        worker,
        "rust",
        payloads,
    )
    assert python_outputs.keys() == rust_outputs.keys()
    for post_id, python_payload in python_outputs.items():
        assert_worker_payload_parity(
            worker,
            python_payload,
            rust_outputs[post_id],
        )


async def _retention_result(
    stack: QualificationStack,
    runtime: Runtime,
    payload: dict[str, Any],
) -> tuple[int, int, int]:
    stack.truncate_worker_state()
    queues = QueueNames.unique(f"storage-{runtime}-retention")
    async with running_worker(stack, "storage", runtime, queues) as broker:
        await broker.publish(queues.scored, payload)
        await _wait_for(
            lambda: stack.retention_snapshot()[0] == 1,
            timeout=60,
            description=f"{runtime} storage did not persist retention fixture",
        )
        await _wait_for(
            lambda: stack.retention_snapshot() == (0, 1, 1),
            timeout=100,
            description=f"{runtime} retention transaction did not complete",
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
    python_result = await _retention_result(
        qualification_stack,
        "python",
        payload,
    )
    rust_result = await _retention_result(
        qualification_stack,
        "rust",
        payload,
    )
    assert rust_result == python_result == (0, 1, 1)


def _observation_artifact_path(worker: Worker) -> Path:
    override = os.getenv("QUALIFICATION_OBSERVE_ARTIFACT")
    if override:
        return Path(override)
    return Path("artifacts") / f"worker-observation-{worker}.json"


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
    # A running container with a quiet queue does not prove the worker still
    # consumes, so the window is bounded by verified probes at their own
    # cadence. The cadence shortens for development runs shorter than four of
    # the configured intervals.
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
    input_queue = queues.input_for(worker)
    output_queue = queues.output_for(worker)
    dead_letter_queue = f"{input_queue}.dead-letter"
    loop = asyncio.get_running_loop()
    samples = 0
    probes = 0
    # Written only when the window completes, so a stale artifact from an
    # earlier run is removed rather than left to be mistaken for this run's
    # evidence.
    artifact = _observation_artifact_path(worker)
    artifact.unlink(missing_ok=True)

    async with running_worker(
        qualification_stack,
        worker,
        "rust",
        queues,
    ) as broker:

        async def probe() -> None:
            """Publish one uniquely identified record and prove it was consumed."""
            nonlocal probes
            payload = copy.deepcopy(qualification_fixtures[worker]["valid"])
            payload["id"] = f"{payload['id']}-observation-{probes:06d}"
            probe_id = payload["id"]
            if worker == "storage":
                # The storage worker rolls up and prunes posts older than
                # POST_RETENTION_DAYS (one day in the qualification topology)
                # 60 seconds after startup and every 24 hours after that. A
                # probe stamped with the recorded fixture time would be
                # archived out of `posts` mid-window, so storage probes carry
                # the current time and survive the whole window.
                payload["timestamp"] = (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
            await broker.publish(input_queue, payload)
            probes += 1
            if output_queue is None:
                await _wait_for(
                    lambda: any(
                        row["id"] == probe_id
                        for row in qualification_stack.post_snapshot()
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

        started_at = time.time()
        await probe()
        deadline = loop.time() + duration
        next_sample = loop.time()
        next_probe = loop.time() + probe_interval
        while loop.time() < deadline:
            now = loop.time()
            if now >= next_sample:
                assert qualification_stack.service_is_running(service)
                assert qualification_stack.queue_state(input_queue) == (0, 0)
                assert qualification_stack.queue_state(dead_letter_queue) == (0, 0), (
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
        # worker that stopped consuming mid-window keeps its container running
        # and its input queue empty, and would otherwise satisfy every
        # assertion above.
        await probe()
        if worker == "storage":
            assert len(qualification_stack.post_snapshot()) == probes
            assert qualification_stack.duplicate_count() == 0

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "worker": worker,
                "runtime": "rust",
                "service": service,
                "started_at": started_at,
                "finished_at": time.time(),
                "duration_hours": duration / 3600,
                "sample_interval_seconds": interval,
                "probe_interval_seconds": probe_interval,
                "liveness_samples": samples,
                "verified_probes": probes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
