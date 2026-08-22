"""Run deterministic producer gates or record a separate timed observation."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCERS = (
    "bluesky",
    "stocktwits",
    "reddit",
    "finnhub",
    "alpaca",
    "market",
    "global-events",
)


def run_checked(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)  # nosec B603


def deterministic_gates(producer: str) -> None:
    run_checked([sys.executable, "scripts/verify_producer_parity.py", "--fixtures-only"])
    run_checked([sys.executable, "-m", "pytest", "-q", "tests/test_producer_fixture_oracles.py"])
    package = {
        "market": "market-producer",
        "global-events": "global-events-producer",
    }.get(producer, "social-news-producer")
    run_checked(["cargo", "test", "--locked", "-p", package])
    run_checked(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.rust.yml",
            "config",
            "--quiet",
        ]
    )


def scrape(url: str) -> dict[str, float]:
    request = urllib.request.Request(url, headers={"User-Agent": "producer-observer/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
        text = response.read().decode("utf-8")
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, raw_value = line.rpartition(" ")
        try:
            metrics[name] = float(raw_value)
        except ValueError:
            continue
    return metrics


def observe(args: argparse.Namespace) -> None:
    if not args.python_metrics_url or not args.rust_metrics_url:
        raise SystemExit("observe requires --python-metrics-url and --rust-metrics-url")
    deadline = time.monotonic() + args.observe_hours * 3600
    samples = []
    while True:
        samples.append(
            {
                "observed_at": time.time(),
                "python": scrape(args.python_metrics_url),
                "rust": scrape(args.rust_metrics_url),
            }
        )
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.sample_seconds, max(0.0, deadline - time.monotonic())))
    report = {
        "producer": args.producer,
        "duration_hours": args.observe_hours,
        "samples": samples,
        "captures": {
            "counts": "posts_ingested_total / provider_requests_total",
            "rate_limits": "rate_limits_hit_total",
            "errors": "provider_requests_total status labels",
            "freshness": "latest successful provider sample timestamp",
            "dlq_growth": "record externally from the isolated RabbitMQ management snapshot",
            "row_payload_divergence": "run verify_producer_parity.py on the paired captures",
        },
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("producer", choices=PRODUCERS)
    parser.add_argument("--mode", choices=("fixtures", "faults", "shadow", "observe", "promotion"), default="fixtures")
    parser.add_argument("--python-jsonl", type=Path)
    parser.add_argument("--rust-jsonl", type=Path)
    parser.add_argument("--key-fields", default="platform,id,symbol")
    parser.add_argument("--replay-count", type=int, default=1000)
    parser.add_argument("--observe-hours", type=float, default=24.0)
    parser.add_argument("--sample-seconds", type=float, default=60.0)
    parser.add_argument("--python-metrics-url")
    parser.add_argument("--rust-metrics-url")
    parser.add_argument("--output", type=Path, default=Path("producer-observation.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.replay_count < 1000 and args.mode in {"promotion", "shadow"}:
        raise SystemExit("promotion/shadow requires at least 1,000 records")
    if args.observe_hours <= 0 or args.sample_seconds <= 0:
        raise SystemExit("observation durations must be positive")
    if args.mode in {"fixtures", "faults", "promotion"}:
        deterministic_gates(args.producer)
    if args.mode in {"shadow", "promotion"}:
        if args.python_jsonl is None or args.rust_jsonl is None:
            raise SystemExit("shadow/promotion requires isolated --python-jsonl and --rust-jsonl captures")
        run_checked(
            [
                sys.executable,
                "scripts/verify_producer_parity.py",
                "--python-jsonl",
                str(args.python_jsonl),
                "--rust-jsonl",
                str(args.rust_jsonl),
                "--limit",
                str(args.replay_count),
                "--key-fields",
                args.key_fields,
            ]
        )
    if args.mode in {"observe", "promotion"}:
        if args.mode == "promotion" and args.observe_hours < 24:
            raise SystemExit("promotion observation must be at least 24 hours")
        observe(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
