"""Docker-backed black-box harness for Python/Rust worker qualification."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess  # nosec B404 - fixed docker/compose command vectors only
import time
import uuid
from base64 import b64encode
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import aio_pika
import psycopg

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.worker-qualification.yml"
Worker = Literal["preprocessing", "sentiment", "storage"]
Runtime = Literal["python", "rust"]

INPUT_QUEUE = {
    "preprocessing": "raw",
    "sentiment": "clean",
    "storage": "scored",
}
OUTPUT_QUEUE = {
    "preprocessing": "clean",
    "sentiment": "scored",
    "storage": None,
}


@dataclass(frozen=True)
class QueueNames:
    raw: str
    clean: str
    scored: str

    @classmethod
    def unique(cls, label: str) -> QueueNames:
        token = uuid.uuid4().hex[:10]
        prefix = f"qualification.{label}.{token}"
        return cls(
            raw=f"{prefix}.raw-posts",
            clean=f"{prefix}.clean-posts",
            scored=f"{prefix}.scored-posts",
        )

    def input_for(self, worker: Worker) -> str:
        return getattr(self, INPUT_QUEUE[worker])

    def output_for(self, worker: Worker) -> str | None:
        output = OUTPUT_QUEUE[worker]
        return getattr(self, output) if output is not None else None

    def environment(self) -> dict[str, str]:
        return {
            "QUAL_QUEUE_RAW": self.raw,
            "QUAL_QUEUE_CLEAN": self.clean,
            "QUAL_QUEUE_SCORED": self.scored,
        }


@dataclass(frozen=True)
class ReceivedMessage:
    body: bytes
    headers: Mapping[str, Any]
    redelivered: bool

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body)
        if not isinstance(value, dict):
            raise TypeError("expected a JSON object message")
        return value


class Broker:
    def __init__(self, url: str):
        self.url = url
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None

    async def __aenter__(self) -> Self:
        connection = await aio_pika.connect_robust(self.url)
        self.connection = connection
        self.channel = await connection.channel(publisher_confirms=True)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.connection is not None:
            await self.connection.close()

    def _channel(self) -> aio_pika.abc.AbstractChannel:
        if self.channel is None:
            raise RuntimeError("Broker must be used as an async context manager")
        return self.channel

    async def declare(self, *queue_names: str) -> None:
        channel = self._channel()
        for name in queue_names:
            await channel.declare_queue(name, durable=True)

    async def delete(self, queue_name: str) -> None:
        queue = await self._channel().declare_queue(
            queue_name,
            passive=True,
        )
        await queue.delete(if_unused=False, if_empty=False)

    async def purge(self, *queue_names: str) -> None:
        channel = self._channel()
        for name in queue_names:
            queue = await channel.declare_queue(name, passive=True)
            await queue.purge()

    async def publish(
        self,
        queue_name: str,
        payload: bytes | str | Mapping[str, Any],
        *,
        headers: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(payload, Mapping):
            body = json.dumps(payload, separators=(",", ":")).encode()
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = payload
        await self._channel().default_exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers=dict(headers or {}),
            ),
            routing_key=queue_name,
            mandatory=True,
        )

    async def receive(
        self,
        queue_name: str,
        *,
        timeout: float = 45.0,
    ) -> ReceivedMessage:
        queue = await self._channel().declare_queue(queue_name, passive=True)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"no message arrived on {queue_name!r}")
            message = await queue.get(
                fail=False,
                timeout=min(remaining, 1.0),
            )
            if message is None:
                continue
            received = ReceivedMessage(
                body=message.body,
                headers=dict(message.headers or {}),
                redelivered=bool(message.redelivered),
            )
            await message.ack()
            return received


class QualificationStack:
    def __init__(self, *, project_name: str | None = None):
        self.project_name = project_name or f"worker-qualification-{uuid.uuid4().hex[:10]}"
        self._started = False

    def _compose(
        self,
        *args: str,
        queue_names: QueueNames | None = None,
        check: bool = True,
        timeout: float = 600,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if queue_names is not None:
            environment.update(queue_names.environment())
        command = [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-name",
            self.project_name,
            *args,
        ]
        completed = subprocess.run(  # nosec B603
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"Docker Compose command failed ({completed.returncode}): "
                f"{' '.join(command)}\n{details}"
            )
        return completed

    def start(self, *, build: bool = False) -> None:
        build_args = ("--build",) if build else ()
        self._compose("up", "-d", "--wait", *build_args, "rabbitmq", "postgres")
        self._started = True
        try:
            self._compose("run", "--rm", *build_args, "schema-migrate")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self._started:
            return
        self._compose(
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
            timeout=180,
        )
        self._started = False

    def host_port(self, service: str, container_port: int) -> int:
        output = self._compose("port", service, str(container_port)).stdout.strip()
        if not output:
            raise RuntimeError(f"no published port for {service}:{container_port}")
        return int(output.rsplit(":", 1)[1])

    @property
    def amqp_url(self) -> str:
        return (
            "amqp://qualification:qualification@127.0.0.1:"
            f"{self.host_port('rabbitmq', 5672)}/"
        )

    @property
    def database_dsn(self) -> str:
        return (
            "postgresql://qualification:qualification@127.0.0.1:"
            f"{self.host_port('postgres', 5432)}/qualification"
        )

    def start_worker(
        self,
        worker: Worker,
        runtime: Runtime,
        queue_names: QueueNames,
        *,
        build: bool = False,
    ) -> str:
        service = f"{runtime}-{worker}"
        build_args = ("--build",) if build else ()
        self._compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *build_args,
            service,
            queue_names=queue_names,
        )
        return service

    def stop_worker(self, worker: Worker, runtime: Runtime) -> None:
        self._compose("stop", "--timeout", "20", f"{runtime}-{worker}")

    def signal_worker(
        self,
        worker: Worker,
        runtime: Runtime,
        signal: str = "SIGTERM",
    ) -> None:
        self._compose("kill", "--signal", signal, f"{runtime}-{worker}")

    def pause_worker(self, worker: Worker, runtime: Runtime) -> None:
        self._compose("pause", f"{runtime}-{worker}")

    def unpause_worker(self, worker: Worker, runtime: Runtime) -> None:
        self._compose("unpause", f"{runtime}-{worker}", check=False)

    def restart_dependency(self, service: Literal["rabbitmq", "postgres"]) -> None:
        self._compose("restart", service)
        self._compose("up", "-d", "--wait", service)

    def stop_dependency(self, service: Literal["rabbitmq", "postgres"]) -> None:
        self._compose("stop", service)

    def start_dependency(self, service: Literal["rabbitmq", "postgres"]) -> None:
        self._compose("start", service)
        self._compose("up", "-d", "--wait", service)

    def service_is_running(self, service: str) -> bool:
        services = self._compose(
            "ps",
            "--status",
            "running",
            "--services",
            check=False,
        ).stdout.splitlines()
        return service in services

    def logs(self, service: str, *, tail: int = 200) -> str:
        return self._compose(
            "logs",
            "--no-color",
            "--tail",
            str(tail),
            service,
            check=False,
        ).stdout

    def queue_state(self, queue_name: str) -> tuple[int, int]:
        authorization = b64encode(b"qualification:qualification").decode()
        request = Request(
            "http://127.0.0.1:"
            f"{self.host_port('rabbitmq', 15672)}/api/queues/%2F/"
            f"{quote(queue_name, safe='')}",
            headers={"Authorization": f"Basic {authorization}"},
        )
        try:
            with urlopen(request, timeout=2) as response:  # nosec B310
                row = json.load(response)
        except HTTPError as error:
            if error.code == 404:
                raise KeyError(
                    f"RabbitMQ queue does not exist: {queue_name}"
                ) from error
            raise
        return int(row["messages_ready"]), int(row["messages_unacknowledged"])

    def wait_for_queue_state(
        self,
        queue_name: str,
        predicate: Any,
        *,
        timeout: float = 45.0,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        last_state: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            try:
                last_state = self.queue_state(queue_name)
            except (KeyError, json.JSONDecodeError, HTTPError, URLError):
                time.sleep(0.05)
                continue
            if predicate(last_state):
                return last_state
            time.sleep(0.05)
        raise TimeoutError(
            f"queue {queue_name!r} did not reach expected state; last={last_state}"
        )

    def direct_queue_state(self, queue_name: str) -> tuple[int, int]:
        """Read exact broker counts without management-plugin sampling lag."""
        output = self._compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--formatter",
            "json",
            "name",
            "messages_ready",
            "messages_unacknowledged",
        ).stdout
        rows = json.loads(output)
        for row in rows:
            if row["name"] == queue_name:
                return (
                    int(row["messages_ready"]),
                    int(row["messages_unacknowledged"]),
                )
        raise KeyError(f"RabbitMQ queue does not exist: {queue_name}")

    def wait_for_direct_queue_state(
        self,
        queue_name: str,
        predicate: Any,
        *,
        timeout: float = 45.0,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        last_state: tuple[int, int] | None = None
        while time.monotonic() < deadline:
            try:
                last_state = self.direct_queue_state(queue_name)
            except (KeyError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if predicate(last_state):
                return last_state
            time.sleep(0.05)
        raise TimeoutError(
            f"queue {queue_name!r} did not reach exact expected state; "
            f"last={last_state}"
        )

    def reject_publishes(self, queue_name: str) -> None:
        pattern = f"^{re.escape(queue_name)}$"
        self._compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "set_policy",
            "qualification-reject-publishes",
            pattern,
            '{"max-length":1,"overflow":"reject-publish"}',
            "--apply-to",
            "queues",
        )

    def clear_publish_rejection(self) -> None:
        self._compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "clear_policy",
            "qualification-reject-publishes",
            check=False,
        )

    def truncate_worker_state(self) -> None:
        with psycopg.connect(self.database_dsn, autocommit=True) as connection:
            connection.execute(
                """
                TRUNCATE posts, hourly_pipeline_quality_facts,
                         hourly_sentiment_agg, hourly_sentiment_facts,
                         raw_post_archive_staging
                RESTART IDENTITY CASCADE
                """
            )

    def post_snapshot(self) -> list[dict[str, Any]]:
        query = """
            SELECT id, symbol, platform, text, timestamp, sentiment, scores,
                   topic_id, topic_label, engagement, ingested_at, cleaned_at,
                   topic_scored_at, sentiment_scored_at,
                   engagement_observed_at, source_schema_version,
                   pipeline_git_commit, topic_model_version,
                   topic_model_hash, sentiment_model_version,
                   sentiment_model_hash
            FROM posts
            ORDER BY platform, id, symbol
        """
        with (
            psycopg.connect(self.database_dsn) as connection,
            connection.cursor(row_factory=psycopg.rows.dict_row) as cursor,
        ):
            cursor.execute(query)
            rows = cursor.fetchall()
        return [_json_normalize(dict(row)) for row in rows]

    def duplicate_count(self) -> int:
        with psycopg.connect(self.database_dsn) as connection:
            value = connection.execute(
                """
                SELECT COALESCE(SUM(count), 0)
                FROM hourly_pipeline_quality_facts
                WHERE pipeline_stage = 'storage'
                  AND reason = 'duplicate_or_conflict'
                """
            ).fetchone()
        return int(value[0]) if value is not None else 0

    def retention_snapshot(self) -> tuple[int, int, int]:
        """Return raw posts, legacy aggregate posts, and analytical fact posts."""
        with psycopg.connect(self.database_dsn) as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM posts),
                    (SELECT COALESCE(SUM(
                        positive_count + neutral_count + negative_count
                    ), 0) FROM hourly_sentiment_agg),
                    (SELECT COALESCE(SUM(post_count), 0)
                     FROM hourly_sentiment_facts)
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("retention snapshot query returned no row")
        return int(row[0]), int(row[1]), int(row[2])


@asynccontextmanager
async def running_worker(
    stack: QualificationStack,
    worker: Worker,
    runtime: Runtime,
    queue_names: QueueNames,
) -> AsyncIterator[Broker]:
    service = stack.start_worker(worker, runtime, queue_names)
    try:
        names = [queue_names.input_for(worker)]
        output = queue_names.output_for(worker)
        if output is not None:
            names.append(output)
        names.append(f"{names[0]}.dead-letter")
        for name in names:
            stack.wait_for_queue_state(name, lambda _state: True, timeout=90.0)
        async with Broker(stack.amqp_url) as broker:
            yield broker
    except Exception as error:
        logs = stack.logs(service)
        if logs:
            error.add_note(f"{service} logs:\n{logs}")
        raise
    finally:
        stack.clear_publish_rejection()
        stack.stop_worker(worker, runtime)

def normalize_worker_payload(worker: Worker, payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_normalize(dict(payload))
    for field in {
        "preprocessing": ("cleaned_at", "topic_scored_at"),
        "sentiment": ("sentiment_scored_at",),
        "storage": (),
    }[worker]:
        normalized.pop(field, None)
    return normalized


def assert_worker_payload_parity(
    worker: Worker,
    python_payload: Mapping[str, Any],
    rust_payload: Mapping[str, Any],
    *,
    probability_tolerance: float = 0.04,
) -> None:
    python_normalized = normalize_worker_payload(worker, python_payload)
    rust_normalized = normalize_worker_payload(worker, rust_payload)
    if worker != "sentiment":
        assert rust_normalized == python_normalized
        return

    python_scores = python_normalized.pop("scores")
    rust_scores = rust_normalized.pop("scores")
    assert rust_normalized == python_normalized
    assert rust_payload["sentiment"] == python_payload["sentiment"]
    assert set(rust_scores) == set(python_scores)
    for label, python_probability in python_scores.items():
        difference = abs(float(rust_scores[label]) - float(python_probability))
        assert difference <= probability_tolerance, (
            f"{label} probability differs by {difference:.6f}, "
            f"above {probability_tolerance:.6f}"
        )


def assert_compatible_dlq_headers(
    headers: Mapping[str, Any],
    *,
    input_queue: str,
    expected_attempt: int | None = None,
) -> None:
    assert headers["x-original-queue"] == input_queue
    assert isinstance(headers["x-error-type"], str)
    assert headers["x-error-type"]
    assert isinstance(headers["x-error"], str)
    assert 0 < len(headers["x-error"]) <= 500
    if expected_attempt is not None:
        assert int(headers["x-processing-attempt"]) == expected_attempt
        assert headers["x-last-error-type"]
        assert headers["x-last-error"]


def _json_normalize(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {key: _json_normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_normalize(item) for item in value]
    return value
