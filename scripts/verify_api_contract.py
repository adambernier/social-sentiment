#!/usr/bin/env python3
"""Black-box Python/Rust API parity gate for a shared seeded database.

Start both implementations against the same PostgreSQL fixture, then provide
their base URLs. The gate compares OpenAPI route/method inventory plus every
read-only application response. Datetimes are normalized and floats use the
migration's absolute 1e-6 tolerance.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
FLOAT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class RequestCase:
    name: str
    path: str
    admin: bool = False


CASES = (
    RequestCase("admin symbols", "/api/admin/symbols", admin=True),
    RequestCase("posts", "/api/posts?symbol=NVDA&limit=25&offset=0"),
    RequestCase("sentiment", "/api/stats/sentiment?symbol=NVDA&hours=24"),
    RequestCase("topics", "/api/stats/topics?symbol=NVDA&hours=24"),
    RequestCase("sources", "/api/stats/sources"),
    RequestCase("leaderboard", "/api/stats/leaderboard"),
    RequestCase("market", "/api/stats/market?symbol=NVDA&hours=24"),
    RequestCase("latest market", "/api/stats/market/latest?symbol=NVDA"),
    RequestCase(
        "market delta",
        "/api/stats/market/delta?symbol=NVDA&since=2020-01-01T00%3A00%3A00Z",
    ),
    RequestCase("metrics", "/api/stats/metrics?symbol=NVDA"),
    RequestCase(
        "global context",
        "/api/stats/global-context?symbol=NVDA&horizon_sessions=30",
    ),
    RequestCase("dashboard", "/api/stats/dashboard?symbol=NVDA&hours=24"),
    RequestCase("correlation", "/api/stats/correlation?symbol=NVDA&hours=24"),
    RequestCase("health", "/api/health"),
)


def fetch(base_url: str, path: str, admin_key: str | None = None) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if admin_key is not None:
        headers["X-API-Key"] = admin_key
    url = base_url.rstrip("/") + path
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("API base URLs must use HTTP or HTTPS")
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)  # nosec B310
    except urllib.error.HTTPError as error:
        payload = error.read()
        return error.code, json.loads(payload) if payload else None
    with response:
        payload = response.read()
        return response.status, json.loads(payload) if payload else None


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str) and TIMESTAMP.fullmatch(value):
        return "<timestamp>"
    return value


def compare(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return [] if expected is actual else [f"{path}: {expected!r} != {actual!r}"]
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if math.isclose(expected, actual, rel_tol=0.0, abs_tol=FLOAT_TOLERANCE):
            return []
        return [f"{path}: {expected!r} != {actual!r}"]
    if type(expected) is not type(actual):
        return [
            f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
        ]
    if isinstance(expected, dict):
        differences = []
        if expected.keys() != actual.keys():
            differences.append(
                f"{path}: keys {sorted(expected)} != {sorted(actual)}"
            )
        for key in expected.keys() & actual.keys():
            differences.extend(compare(expected[key], actual[key], f"{path}.{key}"))
        return differences
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            differences.extend(compare(left, right, f"{path}[{index}]"))
        return differences
    return [] if expected == actual else [f"{path}: {expected!r} != {actual!r}"]


def route_inventory(document: Any) -> dict[str, list[str]]:
    return {
        path: sorted(method for method in operations if method != "parameters")
        for path, operations in document["paths"].items()
    }


def run(python_url: str, rust_url: str, admin_key: str | None) -> int:
    failures = []
    python_openapi = fetch(python_url, "/openapi.json")
    rust_openapi = fetch(rust_url, "/openapi.json")
    if python_openapi[0] != 200 or rust_openapi[0] != 200:
        failures.append("OpenAPI document was not available from both runtimes")
    elif route_inventory(python_openapi[1]) != route_inventory(rust_openapi[1]):
        failures.append("OpenAPI route/method inventory differs")

    for case in CASES:
        key = admin_key if case.admin else None
        python_status, python_body = fetch(python_url, case.path, key)
        rust_status, rust_body = fetch(rust_url, case.path, key)
        if python_status != rust_status:
            failures.append(
                f"{case.name}: status {python_status} != {rust_status}"
            )
            continue
        differences = compare(normalize(python_body), normalize(rust_body))
        failures.extend(f"{case.name}: {difference}" for difference in differences[:20])

    if failures:
        print("API contract parity failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"API contract parity passed for {len(CASES)} requests")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rust-url", default="http://127.0.0.1:8001")
    parser.add_argument("--admin-key")
    arguments = parser.parse_args()
    return run(arguments.python_url, arguments.rust_url, arguments.admin_key)


if __name__ == "__main__":
    raise SystemExit(main())
