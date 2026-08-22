"""Deterministic fixture and isolated capture parity for Rust producers.

This tool never connects to RabbitMQ or PostgreSQL. Captures must come from
shadow queues/tables (or fixture replay), which makes a 1,000-record comparison
safe to run beside production without publishing to production destinations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "providers"
SCENARIOS = {"success", "empty", "malformed", "rate_limit", "error"}
GENERATED_FIELDS = {"ingested_at", "engagement_observed_at", "fetched_at"}


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in sorted(value.items())
            if key not in GENERATED_FIELDS
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def load_jsonl(path: Path, limit: int) -> list[Any]:
    records: list[Any] = []
    with path.open(encoding="utf-8") as capture:
        for line_number, line in enumerate(capture, 1):
            if not line.strip():
                continue
            try:
                records.append(normalize(json.loads(line)))
            except json.JSONDecodeError as error:
                raise AssertionError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if len(records) == limit:
                break
    return records


def record_key(record: Any, fields: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(record, dict):
        raise TypeError("capture records must be JSON objects")
    try:
        return tuple(json.dumps(record[field], sort_keys=True) for field in fields)
    except KeyError as error:
        raise AssertionError(f"capture record is missing key field {error.args[0]!r}") from error


def compare_captures(
    python_path: Path,
    rust_path: Path,
    *,
    limit: int,
    key_fields: tuple[str, ...],
) -> None:
    reference = load_jsonl(python_path, limit)
    candidate = load_jsonl(rust_path, limit)
    if len(reference) != limit or len(candidate) != limit:
        raise AssertionError(
            f"expected {limit} records per runtime; got Python={len(reference)}, Rust={len(candidate)}"
        )
    reference_by_key = {record_key(record, key_fields): record for record in reference}
    candidate_by_key = {record_key(record, key_fields): record for record in candidate}
    if len(reference_by_key) != limit or len(candidate_by_key) != limit:
        raise AssertionError("duplicate identity keys detected in a shadow capture")
    if reference_by_key.keys() != candidate_by_key.keys():
        missing = sorted(reference_by_key.keys() - candidate_by_key.keys())[:5]
        extra = sorted(candidate_by_key.keys() - reference_by_key.keys())[:5]
        raise AssertionError(f"capture keys diverged; missing={missing}, extra={extra}")
    for key, expected in reference_by_key.items():
        actual = candidate_by_key[key]
        if actual != expected:
            raise AssertionError(
                f"normalized capture diverged for {key}:\n"
                f"Python={json.dumps(expected, sort_keys=True)}\n"
                f"Rust={json.dumps(actual, sort_keys=True)}"
            )


def validate_fixtures() -> None:
    providers = sorted(FIXTURES.glob("*/cases.json"))
    if not providers:
        raise AssertionError("no provider fixtures found")
    for fixture_path in providers:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if set(fixture.get("responses", {})) != SCENARIOS:
            raise AssertionError(f"{fixture_path} must define exactly {sorted(SCENARIOS)}")
        request_text = json.dumps(fixture.get("request", {})).lower()
        for forbidden in ("api-secret-key\":\"sk-", "token\":\"sk-", "bearer "):
            if forbidden in request_text:
                raise AssertionError(f"{fixture_path} contains an unsanitized secret")
        if "expected" not in fixture or not isinstance(fixture["expected"], list):
            raise AssertionError(f"{fixture_path} is missing normalized expected output")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-jsonl", type=Path)
    parser.add_argument("--rust-jsonl", type=Path)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--key-fields",
        default="platform,id,symbol",
        help="Comma-separated identity fields; use database primary-key columns for row captures",
    )
    parser.add_argument("--fixtures-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_fixtures()
    if args.fixtures_only:
        print("provider fixture contract: PASS")
        return 0
    if args.limit < 1:
        raise SystemExit("--limit must be at least one")
    if args.python_jsonl is None or args.rust_jsonl is None:
        raise SystemExit("both --python-jsonl and --rust-jsonl are required for shadow parity")
    key_fields = tuple(field.strip() for field in args.key_fields.split(",") if field.strip())
    if not key_fields:
        raise SystemExit("--key-fields must contain at least one field")
    compare_captures(
        args.python_jsonl,
        args.rust_jsonl,
        limit=args.limit,
        key_fields=key_fields,
    )
    print(f"producer shadow parity: PASS ({args.limit} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
