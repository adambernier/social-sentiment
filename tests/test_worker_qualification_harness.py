import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.worker_qualification import evidence
from tests.worker_qualification.evidence import (
    artifact_path,
    evidence_artifact,
    git_revision,
)
from tests.worker_qualification.harness import (
    PROBABILITY_TOLERANCE,
    QualificationStack,
    QueueNames,
    assert_compatible_dlq_headers,
    assert_regression_within_tolerance,
    assert_worker_payload_parity,
    latency_summary,
    margin_record,
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

    # The largest delta across labels, which the replay gate records as its
    # margin, not the first one it happened to compare.
    assert assert_worker_payload_parity("sentiment", reference, candidate) == (
        pytest.approx(0.03)
    )

    candidate["scores"]["positive"] = 0.70
    with pytest.raises(AssertionError, match="above 0.040000"):
        assert_worker_payload_parity("sentiment", reference, candidate)


def test_exact_payload_parity_reports_no_delta():
    reference = {"id": "post-1", "topic_model_hash": "same-hash"}
    candidate = deepcopy(reference)
    candidate["cleaned_at"] = "rust-time"

    assert assert_worker_payload_parity("preprocessing", reference, candidate) == 0.0

    candidate["topic_model_hash"] = "other-hash"
    with pytest.raises(AssertionError):
        assert_worker_payload_parity("preprocessing", reference, candidate)


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


def test_direct_queue_state_uses_exact_broker_counts(monkeypatch):
    stack = QualificationStack(project_name="stability-test")
    output = """[
      {"name":"qualification.input","messages_ready":0,
       "messages_unacknowledged":1}
    ]"""
    monkeypatch.setattr(
        stack,
        "_compose",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )

    assert stack.direct_queue_state("qualification.input") == (0, 1)


def test_latency_summary_reports_nearest_rank_percentiles():
    summary = latency_summary([0.040, 0.010, 0.030, 0.020])

    assert summary["count"] == 4
    assert summary["min_ms"] == pytest.approx(10.0)
    assert summary["mean_ms"] == pytest.approx(25.0)
    assert summary["p50_ms"] == pytest.approx(20.0)
    assert summary["p95_ms"] == pytest.approx(40.0)
    assert summary["p99_ms"] == pytest.approx(40.0)
    assert summary["max_ms"] == pytest.approx(40.0)


def test_latency_summary_rejects_an_empty_sample():
    with pytest.raises(ValueError, match="no latency samples"):
        latency_summary([])


def test_regression_tolerance_blocks_only_material_regressions():
    within = assert_regression_within_tolerance(
        100.0, 109.0, metric="p95_ms", tolerance=0.10, floor=5.0, label="p95"
    )

    assert within == {
        "metric": "p95_ms",
        "observed": 109.0,
        "limit": 110.0,
        "headroom": 1.0,
        "reference": 100.0,
        "tolerance": 0.10,
        "allowed": 110.0,
        "floor": 5.0,
        "bound_by": "tolerance",
    }

    with pytest.raises(AssertionError, match="p95 regressed"):
        assert_regression_within_tolerance(
            100.0, 111.0, metric="p95_ms", tolerance=0.10, floor=5.0, label="p95"
        )

    # Below the floor a large relative delta is still a trivial absolute one, so
    # the margin reports the floor as the binding limit rather than a headroom
    # that would read as negative in a passing record.
    trivial = assert_regression_within_tolerance(
        1.0, 3.0, metric="p95_ms", tolerance=0.10, floor=5.0, label="p95"
    )

    assert trivial["bound_by"] == "floor"
    assert trivial["limit"] == 5.0
    assert trivial["headroom"] == 2.0


def test_margin_record_states_the_headroom_a_gate_passed_by():
    exact = margin_record("max_normalized_payload_delta", 0.0, 0.0)

    assert exact == {
        "metric": "max_normalized_payload_delta",
        "observed": 0.0,
        "limit": 0.0,
        "headroom": 0.0,
    }
    assert margin_record(
        "max_probability_delta", 0.014, PROBABILITY_TOLERANCE
    )["headroom"] == pytest.approx(PROBABILITY_TOLERANCE - 0.014)


def test_container_image_provenance_names_the_image_that_ran(monkeypatch):
    stack = QualificationStack(project_name="provenance-test")
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {
                "Service": "rust-preprocessing",
                "Image": "qualification-abc-rust-preprocessing:latest",
                "State": "exited",
                "Labels": (
                    "com.docker.compose.project=qualification-abc,"
                    "com.docker.compose.image=sha256:cafe,"
                    "com.docker.compose.service=rust-preprocessing"
                ),
            },
            {
                "Service": "postgres",
                "Image": "postgres:15",
                "State": "running",
                "Labels": "com.docker.compose.image=sha256:beef",
            },
        )
    )
    monkeypatch.setattr(
        stack,
        "_compose",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=rows),
    )

    assert stack.container_image_provenance(["rust-preprocessing"]) == {
        "rust-preprocessing": {
            "image": "qualification-abc-rust-preprocessing:latest",
            "image_id": "sha256:cafe",
            "state": "exited",
        }
    }


def test_artifact_path_defaults_per_gate_and_honours_overrides(monkeypatch):
    monkeypatch.delenv("QUALIFICATION_LOAD_ARTIFACT", raising=False)

    assert artifact_path("load", "sentiment") == Path(
        "artifacts/worker-load-sentiment.json"
    )
    assert artifact_path("replay", "storage") == Path(
        "artifacts/worker-replay-storage.json"
    )

    monkeypatch.setenv("QUALIFICATION_LOAD_ARTIFACT", "/tmp/load.json")
    assert artifact_path("load", "sentiment") == Path("/tmp/load.json")

    with pytest.raises(ValueError, match="unknown evidence artifact kind"):
        artifact_path("promotion", "sentiment")


def test_evidence_artifact_records_a_pass_and_drops_stale_evidence(tmp_path):
    path = tmp_path / "worker-load-preprocessing.json"
    path.write_text(json.dumps({"status": "passed", "messages": 1}))

    with evidence_artifact(path, {"gate": "load", "worker": "preprocessing"}) as record:
        record["messages"] = 2000

    written = json.loads(path.read_text())
    assert written["status"] == "passed"
    assert written["messages"] == 2000
    assert written["finished_at"] >= written["started_at"]


def test_evidence_artifact_retains_a_failed_gate(tmp_path):
    path = tmp_path / "worker-load-sentiment.json"

    with pytest.raises(RuntimeError, match="p95 regressed"):
        with evidence_artifact(path, {"gate": "load"}) as record:
            record["candidate"] = {"latency": {"p95_ms": 90.0}}
            raise RuntimeError("p95 regressed")

    written = json.loads(path.read_text())
    assert written["status"] == "failed"
    assert "p95 regressed" in written["error"]
    assert written["candidate"] == {"latency": {"p95_ms": 90.0}}


def test_stack_project_name_precedence(monkeypatch):
    monkeypatch.delenv("QUALIFICATION_PROJECT_NAME", raising=False)
    generated = QualificationStack().project_name
    assert generated.startswith("worker-qualification-")
    assert QualificationStack().project_name != generated

    monkeypatch.setenv("QUALIFICATION_PROJECT_NAME", "worker-qualification-reused")
    assert (
        QualificationStack().project_name == "worker-qualification-reused"
    )
    assert (
        QualificationStack(project_name="worker-qualification-explicit").project_name
        == "worker-qualification-explicit"
    )


def test_git_revision_reports_an_unavailable_checkout(monkeypatch):
    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("git is not installed")

    monkeypatch.setattr(evidence.subprocess, "run", explode)

    assert git_revision() == {"commit": None, "dirty": None}
