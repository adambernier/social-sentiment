"""Capture published messages from an isolated shadow queue into JSONL.

The producer shadow-parity comparator (``scripts/verify_producer_parity.py``)
reads one JSONL capture per runtime. This tool produces those captures from
the isolated shadow queues a runtime publishes to during a replay run: it
consumes exactly ``--count`` messages, validates each one is a JSON object,
acknowledges it, and writes one line per message.

The capture is written to ``<output>.partial`` and renamed only after the
requested count is reached, so a truncated file can never be mistaken for a
complete capture. On timeout or interrupt the partial file is kept (with the
unacknowledged remainder still on the queue) and the exit status is non-zero.

The queue is declared durable to match the producers' declaration; the broker
must be the isolated qualification/shadow broker, never production.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aio_pika  # noqa: E402

from shared.config import (  # noqa: E402
    RABBIT_HOST,
    RABBIT_PASS,
    RABBIT_PORT,
    RABBIT_USER,
)

LOG = logging.getLogger("capture-shadow-queue")


class CaptureError(RuntimeError):
    """A captured message could not be validated."""


class CaptureTimeout(CaptureError):
    """The requested count was not reached before the deadline."""


def decode_message(body: bytes) -> str:
    """Validate one queue message and return it as a single JSONL line."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaptureError(f"queue message is not valid UTF-8: {error}") from error
    try:
        record = json.loads(text)
    except json.JSONDecodeError as error:
        raise CaptureError(f"queue message is not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise CaptureError("queue message is not a JSON object")
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


async def drain_messages(
    queue: Any,
    count: int,
    captured: list[str],
    *,
    timeout: float,
    poll_interval: float = 0.2,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Consume and acknowledge messages until ``count`` are captured.

    Each message is acknowledged only after its JSON line is appended to
    ``captured``, so an interrupted capture leaves the remainder on the queue.
    """
    if count < 1:
        raise CaptureError("count must be at least 1")
    deadline = None if timeout <= 0 else now() + timeout
    while len(captured) < count:
        if deadline is not None and now() >= deadline:
            raise CaptureTimeout(
                f"captured {len(captured)} of {count} messages before the timeout"
            )
        message = await queue.get(no_ack=False, fail=False, timeout=poll_interval)
        if message is None:
            continue
        line = decode_message(message.body)
        captured.append(line)
        await message.ack()
        if len(captured) % 100 == 0:
            LOG.info("captured %d/%d messages", len(captured), count)


def partial_path(output: Path) -> Path:
    return output.with_name(output.name + ".partial")


def write_partial(output: Path, lines: list[str]) -> Path:
    path = partial_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def write_capture(output: Path, lines: list[str]) -> Path:
    path = write_partial(output, lines)
    os.replace(path, output)
    return output


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_report(
    *,
    queue: str,
    requested: int,
    output: Path,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "queue": queue,
        "requested": requested,
        "captured": requested,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "sha256": file_sha256(output),
        "output": str(output),
        "host": {"hostname": socket.gethostname(), "platform": platform.platform()},
        "finished_at": time.time(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, help="isolated shadow queue to drain")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rabbit-url", help="AMQP URL; overrides host/port/user/password")
    parser.add_argument("--host", default=RABBIT_HOST)
    parser.add_argument("--port", type=int, default=RABBIT_PORT)
    parser.add_argument("--user", default=RABBIT_USER)
    parser.add_argument("--password", default=RABBIT_PASS)
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="seconds to wait for the full count; 0 waits indefinitely (default)",
    )
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


async def _capture(args: argparse.Namespace, captured: list[str]) -> None:
    url = args.rabbit_url or f"amqp://{args.user}:{args.password}@{args.host}:{args.port}/"
    connection = await aio_pika.connect_robust(url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(args.queue, durable=True)
        LOG.info("capturing %d messages from %r", args.count, args.queue)
        await drain_messages(
            queue,
            args.count,
            captured,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.count < 1:
        raise SystemExit("--count must be at least 1")
    if not args.queue:
        raise SystemExit("--queue must not be empty")

    captured: list[str] = []
    started = time.monotonic()
    try:
        asyncio.run(_capture(args, captured))
    except CaptureTimeout as error:
        partial = write_partial(args.output, captured)
        raise SystemExit(f"{error}; partial capture kept at {partial}") from None
    except CaptureError as error:
        partial = write_partial(args.output, captured)
        raise SystemExit(f"{error}; partial capture kept at {partial}") from None
    except KeyboardInterrupt:
        partial = write_partial(args.output, captured)
        raise SystemExit(
            f"interrupted after {len(captured)} messages; partial capture kept at {partial}"
        ) from None
    except (aio_pika.exceptions.AMQPError, OSError) as error:
        raise SystemExit(f"could not capture from rabbitmq: {error}") from None

    output = write_capture(args.output, captured)
    report = build_report(
        queue=args.queue,
        requested=args.count,
        output=output,
        elapsed_seconds=time.monotonic() - started,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
