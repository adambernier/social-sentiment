"""Offline coverage for the shadow-queue capture tool."""

from __future__ import annotations

import json

import pytest

from scripts.capture_shadow_queue import (
    CaptureError,
    CaptureTimeout,
    decode_message,
    drain_messages,
    write_capture,
    write_partial,
)


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


class FakeQueue:
    def __init__(self, bodies: list[bytes]) -> None:
        self.messages = [FakeMessage(body) for body in bodies]
        self.served: list[FakeMessage] = []

    async def get(self, *, no_ack: bool = False, fail: bool = True, timeout=None):
        if not self.messages:
            return None
        message = self.messages.pop(0)
        self.served.append(message)
        return message


class StepClock:
    """A monotonic clock that advances one second per read."""

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_decode_message_returns_compact_json_line():
    line = decode_message(b'{"platform": "bluesky",  "id": "x"}')
    assert json.loads(line) == {"platform": "bluesky", "id": "x"}
    assert line.endswith("\n")


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_decode_message_rejects_invalid_messages(body: bytes):
    with pytest.raises(CaptureError):
        decode_message(body)


async def test_drain_captures_and_acknowledges_exact_count():
    queue = FakeQueue([b'{"id": "one"}', b'{"id": "two"}'])
    captured: list[str] = []
    await drain_messages(queue, 2, captured, timeout=0)
    assert [json.loads(line)["id"] for line in captured] == ["one", "two"]
    assert all(message.acked for message in queue.served)


async def test_drain_times_out_with_the_partial_capture_in_hand():
    queue = FakeQueue([b'{"id": "one"}'])
    captured: list[str] = []
    with pytest.raises(CaptureTimeout, match="captured 1 of 2"):
        await drain_messages(queue, 2, captured, timeout=3.0, now=StepClock())
    assert len(captured) == 1
    assert queue.served[0].acked


async def test_drain_leaves_invalid_messages_unacknowledged():
    queue = FakeQueue([b"not json"])
    captured: list[str] = []
    with pytest.raises(CaptureError):
        await drain_messages(queue, 1, captured, timeout=0)
    assert captured == []
    assert not queue.served[0].acked


async def test_drain_rejects_non_positive_count():
    with pytest.raises(CaptureError):
        await drain_messages(FakeQueue([]), 0, [], timeout=0)


def test_write_capture_renames_the_partial_only_on_success(tmp_path):
    output = tmp_path / "capture.jsonl"
    partial = write_partial(output, ['{"id": "one"}\n'])
    assert partial.name == "capture.jsonl.partial"
    assert partial.exists() and not output.exists()

    final = write_capture(output, ['{"id": "one"}\n'])
    assert final == output
    assert output.read_text(encoding="utf-8") == '{"id": "one"}\n'
    assert not partial.exists()
