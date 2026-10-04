"""Manually triggered replay, load, retention, and observation promotion gates."""

from __future__ import annotations

import asyncio
import copy
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from .evidence import artifact_path, evidence_artifact, git_revision, host_platform
from .harness import (
    PROBABILITY_TOLERANCE,
    Broker,
    QualificationStack,
    QueueNames,
    Runtime,
    Worker,
    assert_regression_within_tolerance,
    assert_worker_payload_parity,
    latency_summary,
    margin_record,
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


def _payload_batch(
    fixture: dict[str, Any],
    count: int,
    prefix: str,
) -> list[dict[str, Any]]:
    payloads = []
    for index in range(count):
        payload = copy.deepcopy(fixture)
        payload["id"] = f"{prefix}-{index:06d}"
        payloads.append(payload)
    return payloads


def _replay_payloads(
    fixture: dict[str, Any],
    count: int,
) -> list[dict[str, Any]]:
    return _payload_batch(fixture, count, "qualification-replay")


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
    record = _evidence_record(
        worker,
        "replay",
        f"rust-{worker}",
        qualification_stack.project_name,
    )
    record["message_count"] = count
    record["comparison"] = "python_vs_rust"
    with evidence_artifact(artifact_path("replay", worker), record) as evidence:
        if worker == "storage":
            python_state, python_duplicates = await _storage_replay(
                qualification_stack,
                "python",
                payloads,
            )
            rust_state, rust_duplicates = await _storage_replay(
                qualification_stack,
                "rust",
                payloads,
            )
            assert rust_state == python_state
            evidence["reference"] = {
                "rows": len(python_state),
                "duplicate_rows": python_duplicates,
            }
            evidence["candidate"] = {
                "rows": len(rust_state),
                "duplicate_rows": rust_duplicates,
            }
            evidence["margin"] = margin_record(
                "persisted_row_difference",
                0.0,
                0.0,
                criterion="candidate rows and duplicate accounting equal the reference",
            )
        else:
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
            largest_delta = 0.0
            for post_id, python_payload in python_outputs.items():
                largest_delta = max(
                    largest_delta,
                    assert_worker_payload_parity(
                        worker,
                        python_payload,
                        rust_outputs[post_id],
                    ),
                )
            evidence["reference"] = {"messages": len(python_outputs)}
            evidence["candidate"] = {"messages": len(rust_outputs)}
            evidence["payloads_compared"] = len(python_outputs)
            evidence["mismatches"] = 0
            if worker == "sentiment":
                evidence["margin"] = margin_record(
                    "max_probability_delta",
                    largest_delta,
                    PROBABILITY_TOLERANCE,
                    criterion="every label within the probability tolerance",
                )
            else:
                # The comparison is exact equality, so the delta is zero by
                # construction and the limit admits no deviation at all.
                evidence["margin"] = margin_record(
                    "max_normalized_payload_delta",
                    largest_delta,
                    0.0,
                    criterion="every normalized payload equal to the reference",
                )
        evidence["images"] = qualification_stack.container_image_provenance(
            [f"python-{worker}", f"rust-{worker}"]
        )


async def _warmup(
    stack: QualificationStack,
    broker: Broker,
    queues: QueueNames,
    worker: Worker,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> None:
    """Send one message and wait for it, keeping warm-up out of the samples."""
    payload = copy.deepcopy(payload)
    payload["id"] = f"{payload['id']}-load-warmup"
    output_queue = queues.output_for(worker)
    await broker.publish(queues.input_for(worker), payload)
    if output_queue is None:
        await _wait_for(
            lambda: stack.post_count() >= 1,
            timeout=timeout,
            description="storage worker did not persist the warm-up message",
        )
        return
    output = (await broker.receive(output_queue, timeout=timeout)).json()
    assert output["id"] == payload["id"]


async def _publish_paced(
    broker: Broker,
    queue_name: str,
    payloads: list[dict[str, Any]],
    *,
    rate: float,
    sent_at: dict[str, float],
) -> None:
    """Publish a sustained load, stamping each message as it is handed over."""
    interval = 1.0 / rate if rate > 0 else 0.0
    loop = asyncio.get_running_loop()
    start = loop.time()
    for index, payload in enumerate(payloads):
        # Stamped before the publish returns: the broker can deliver a message
        # before its publisher confirm arrives, and a later stamp would race the
        # consumer for the identifier, losing a sample.
        sent_at[payload["id"]] = time.time()
        await broker.publish(queue_name, payload)
        if interval:
            delay = start + (index + 1) * interval - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)


async def _collect(
    broker: Broker,
    queue_name: str,
    received_at: dict[str, float],
    expected: int,
) -> None:
    async for message in broker.consume(queue_name):
        received_at[message.json()["id"]] = time.time()
        if len(received_at) >= expected:
            return


async def _load_run(
    stack: QualificationStack,
    worker: Worker,
    runtime: Runtime,
    payloads: list[dict[str, Any]],
    *,
    rate: float,
    timeout: float,
) -> dict[str, Any]:
    """Publish one sustained load and measure how the runtime absorbs it."""
    if worker == "storage":
        stack.truncate_worker_state()
    queues = QueueNames.unique(f"{worker}-{runtime}-load")
    input_queue = queues.input_for(worker)
    output_queue = queues.output_for(worker)
    sent_at: dict[str, float] = {}
    received_at: dict[str, float] = {}

    async with running_worker(stack, worker, runtime, queues) as broker:
        await _warmup(stack, broker, queues, worker, payloads[0], timeout=timeout)
        warmup_rows = stack.post_count() if worker == "storage" else 0
        started = time.time()
        consumer: asyncio.Task[None] | None = None
        if output_queue is not None:
            consumer = asyncio.create_task(
                _collect(broker, output_queue, received_at, len(payloads))
            )
        await _publish_paced(
            broker,
            input_queue,
            payloads,
            rate=rate,
            sent_at=sent_at,
        )
        published_at = time.time()
        if consumer is not None:
            await asyncio.wait_for(consumer, timeout=timeout)
        else:
            await _wait_for(
                lambda: stack.post_count() >= warmup_rows + len(payloads),
                timeout=timeout,
                description=f"{runtime} storage worker did not persist the load",
            )
        handled_at = time.time()
        stack.wait_for_queue_state(
            input_queue,
            lambda state: state == (0, 0),
            timeout=120,
        )
        dead_letter = stack.queue_state(f"{input_queue}.dead-letter")
        rows = stack.post_count() if worker == "storage" else 0
        duplicates = stack.duplicate_count() if worker == "storage" else 0

    assert dead_letter == (0, 0), (
        f"{runtime} sustained load dead-lettered messages: {dead_letter}"
    )
    elapsed = handled_at - started
    drain = handled_at - published_at
    result: dict[str, Any] = {
        "runtime": runtime,
        "messages": len(payloads),
        "publish_seconds": round(published_at - started, 3),
        "elapsed_seconds": round(elapsed, 3),
        # The publish pace pins the total, so the tail past the last publish is
        # what separates a runtime that keeps up from one that falls behind.
        "drain_seconds": round(drain, 3),
        "absorbed_per_second": round(len(payloads) / max(elapsed, 1e-9), 3),
        "dead_letter_depth": list(dead_letter),
    }
    if worker == "storage":
        assert rows == warmup_rows + len(payloads), (
            f"{runtime} persisted {rows} rows for {len(payloads)} messages"
        )
        assert duplicates == 0, f"{runtime} recorded {duplicates} duplicate rows"
        result["rows"] = rows
        result["duplicate_rows"] = duplicates
    else:
        assert set(received_at) == set(sent_at), (
            f"{runtime} handled {len(received_at)} of {len(sent_at)} messages"
        )
        result["latency"] = latency_summary(
            [received_at[payload["id"]] - sent_at[payload["id"]] for payload in payloads]
        )
    return result


async def test_sustained_load_gate(
    qualification_stack: QualificationStack,
    qualification_fixtures: dict[str, dict[str, Any]],
) -> None:
    """Compare both runtimes under the same sustained publish rate.

    The observation window proves a worker stays alive at one message every few
    minutes; this gate proves it keeps up. Rust may not regress past the
    tolerance on p95 end-to-end latency, or, for the storage worker, which has
    no output queue to time against, on the time it takes to absorb the load.
    """
    count = int(os.getenv("QUALIFICATION_LOAD_COUNT", "0"))
    if count <= 0:
        pytest.skip("set QUALIFICATION_LOAD_COUNT (normally 2000) to run")
    rate = float(os.getenv("QUALIFICATION_LOAD_RATE", "50"))
    if rate <= 0:
        raise ValueError("QUALIFICATION_LOAD_RATE must be greater than zero")
    tolerance = float(os.getenv("QUALIFICATION_LOAD_P95_TOLERANCE", "0.10"))
    latency_floor_ms = float(os.getenv("QUALIFICATION_LOAD_LATENCY_FLOOR_MS", "5"))
    duration_floor_seconds = float(
        os.getenv("QUALIFICATION_LOAD_DURATION_FLOOR_SECONDS", "2")
    )
    timeout = max(300.0, count / rate * 4)
    worker = _selected_worker()
    payloads = _payload_batch(
        qualification_fixtures[worker]["valid"],
        count,
        "qualification-load",
    )
    if worker == "storage":
        # The storage worker prunes posts older than POST_RETENTION_DAYS (one day
        # in the qualification topology), so probe timestamps must be current or
        # the load is archived mid-gate.
        for payload in payloads:
            payload["timestamp"] = _current_timestamp()

    record = _evidence_record(
        worker,
        "load",
        f"rust-{worker}",
        qualification_stack.project_name,
    )
    record["message_count"] = count
    record["publish_rate_per_second"] = rate
    record["thresholds"] = {
        "p95_tolerance": tolerance,
        "latency_floor_ms": latency_floor_ms,
        "duration_floor_seconds": duration_floor_seconds,
    }
    with evidence_artifact(artifact_path("load", worker), record) as evidence:
        reference = await _load_run(
            qualification_stack,
            worker,
            "python",
            payloads,
            rate=rate,
            timeout=timeout,
        )
        candidate = await _load_run(
            qualification_stack,
            worker,
            "rust",
            payloads,
            rate=rate,
            timeout=timeout,
        )
        evidence["reference"] = reference
        evidence["candidate"] = candidate
        evidence["images"] = qualification_stack.container_image_provenance(
            [f"python-{worker}", f"rust-{worker}"]
        )
        if worker == "storage":
            evidence["margin"] = assert_regression_within_tolerance(
                reference["drain_seconds"],
                candidate["drain_seconds"],
                metric="drain_seconds",
                tolerance=tolerance,
                floor=duration_floor_seconds,
                label=f"{worker} drain time",
            )
        else:
            evidence["margin"] = assert_regression_within_tolerance(
                reference["latency"]["p95_ms"],
                candidate["latency"]["p95_ms"],
                metric="p95_ms",
                tolerance=tolerance,
                floor=latency_floor_ms,
                label=f"{worker} p95 latency",
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
    """Hold one runtime in a live window and prove it keeps consuming.

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
