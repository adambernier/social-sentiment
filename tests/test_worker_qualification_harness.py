from copy import deepcopy

import pytest

from tests.worker_qualification import harness
from tests.worker_qualification.harness import (
    QualificationStack,
    QueueNames,
    assert_compatible_dlq_headers,
    assert_worker_payload_parity,
    normalize_worker_payload,
)


def test_qualification_queues_are_unique_and_stage_aware():
    first = QueueNames.unique("preprocessing")
    second = QueueNames.unique("preprocessing")

    assert first != second
    assert first.input_for("preprocessing") == first.raw
    assert first.output_for("preprocessing") == first.clean
    assert first.input_for("sentiment") == first.clean
    assert first.output_for("sentiment") == first.scored
    assert first.input_for("storage") == first.scored
    assert first.output_for("storage") is None
    assert first.environment() == {
        "QUAL_QUEUE_RAW": first.raw,
        "QUAL_QUEUE_CLEAN": first.clean,
        "QUAL_QUEUE_SCORED": first.scored,
    }


def test_payload_normalization_ignores_only_runtime_timestamps():
    payload = {
        "id": "post-1",
        "cleaned_at": "python-time",
        "topic_scored_at": "python-time",
        "topic_model_hash": "same-hash",
    }

    assert normalize_worker_payload("preprocessing", payload) == {
        "id": "post-1",
        "topic_model_hash": "same-hash",
    }


def test_sentiment_parity_enforces_labels_and_probability_tolerance():
    reference = {
        "id": "post-1",
        "sentiment": "positive",
        "scores": {"positive": 0.8, "neutral": 0.15, "negative": 0.05},
        "sentiment_scored_at": "python-time",
    }
    candidate = deepcopy(reference)
    candidate["sentiment_scored_at"] = "rust-time"
    candidate["scores"]["positive"] = 0.77
    candidate["scores"]["neutral"] = 0.18

    assert_worker_payload_parity("sentiment", reference, candidate)

    candidate["scores"]["positive"] = 0.70
    with pytest.raises(AssertionError, match="above 0.040000"):
        assert_worker_payload_parity("sentiment", reference, candidate)


def test_dlq_header_contract_includes_retry_history():
    assert_compatible_dlq_headers(
        {
            "x-original-queue": "test.raw",
            "x-error-type": "ProcessingError",
            "x-error": "publisher rejected message",
            "x-processing-attempt": 1,
            "x-last-error-type": "ProcessingError",
            "x-last-error": "publisher rejected message",
        },
        input_queue="test.raw",
        expected_attempt=1,
    )


def test_queue_state_wait_requires_continuous_stability(monkeypatch):
    stack = QualificationStack(project_name="stability-test")
    states = iter([(0, 0), (1, 0), (0, 0), (0, 0), (0, 0), (0, 0)])
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        now += duration

    monkeypatch.setattr(stack, "queue_state", lambda _queue: next(states))
    monkeypatch.setattr(harness.time, "monotonic", monotonic)
    monkeypatch.setattr(harness.time, "sleep", sleep)

    assert stack.wait_for_queue_state(
        "qualification.input",
        lambda state: state == (0, 0),
        stable_for=0.1,
    ) == (0, 0)
    assert now >= 0.2
