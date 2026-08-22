import json

import pytest

from scripts.verify_producer_parity import compare_captures, normalize


def test_normalization_removes_only_generated_ingestion_fields():
    assert normalize(
        {
            "id": "one",
            "timestamp": "provider-time",
            "occurred_at": "event-time",
            "ingested_at": "runtime-time",
            "engagement_observed_at": "runtime-time",
            "nested": {"fetched_at": "runtime-time", "value": 3},
        }
    ) == {
        "id": "one",
        "timestamp": "provider-time",
        "occurred_at": "event-time",
        "nested": {"value": 3},
    }


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_capture_comparison_is_order_independent_and_exact(tmp_path):
    python_capture = tmp_path / "python.jsonl"
    rust_capture = tmp_path / "rust.jsonl"
    reference = [
        {"platform": "reddit", "id": "one", "symbol": "AAPL", "value": 1},
        {"platform": "reddit", "id": "two", "symbol": "MSFT", "value": 2},
    ]
    candidate = list(reversed(reference))
    candidate[0] = {**candidate[0], "ingested_at": "different"}
    write_jsonl(python_capture, reference)
    write_jsonl(rust_capture, candidate)
    compare_captures(
        python_capture,
        rust_capture,
        limit=2,
        key_fields=("platform", "id", "symbol"),
    )

    candidate[0]["value"] = 99
    write_jsonl(rust_capture, candidate)
    with pytest.raises(AssertionError, match="normalized capture diverged"):
        compare_captures(
            python_capture,
            rust_capture,
            limit=2,
            key_fields=("platform", "id", "symbol"),
        )


def test_capture_comparison_rejects_duplicate_identity(tmp_path):
    python_capture = tmp_path / "python.jsonl"
    rust_capture = tmp_path / "rust.jsonl"
    duplicate = [
        {"platform": "reddit", "id": "one", "symbol": "AAPL"},
        {"platform": "reddit", "id": "one", "symbol": "AAPL"},
    ]
    write_jsonl(python_capture, duplicate)
    write_jsonl(rust_capture, duplicate)
    with pytest.raises(AssertionError, match="duplicate identity keys"):
        compare_captures(
            python_capture,
            rust_capture,
            limit=2,
            key_fields=("platform", "id", "symbol"),
        )
