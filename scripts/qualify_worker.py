"""Run an isolated worker qualification or staged promotion gate."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - fixed pytest command vector
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify one worker against isolated RabbitMQ/PostgreSQL containers. "
            "Python remains the behavioral oracle and production default."
        )
    )
    parser.add_argument(
        "worker",
        choices=("preprocessing", "sentiment", "storage"),
    )
    parser.add_argument(
        "--mode",
        choices=(
            "smoke",
            "faults",
            "replay",
            "load",
            "retention",
            "observe",
            "promotion",
        ),
        default="smoke",
    )
    parser.add_argument("--replay-count", type=int, default=1000)
    parser.add_argument(
        "--load-count",
        type=int,
        default=2000,
        help="Messages per runtime for the sustained-load gate (default: 2000)",
    )
    parser.add_argument(
        "--load-rate",
        type=float,
        default=50.0,
        help="Sustained publish rate per second for the load gate (default: 50)",
    )
    parser.add_argument(
        "--load-tolerance",
        type=float,
        default=0.10,
        help="Allowed p95 regression for Rust versus Python (default: 0.10)",
    )
    parser.add_argument(
        "--observe-hours",
        type=float,
        default=24.0,
        help="Observation duration for observe/promotion mode (default: 24)",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Rebuild qualification images before starting",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.replay_count < 1:
        raise SystemExit("--replay-count must be at least 1")
    if args.observe_hours <= 0:
        raise SystemExit("--observe-hours must be greater than zero")
    if args.load_count < 1:
        raise SystemExit("--load-count must be at least 1")
    if args.load_rate <= 0:
        raise SystemExit("--load-rate must be greater than zero")
    if args.load_tolerance < 0:
        raise SystemExit("--load-tolerance must not be negative")

    marker = {
        "smoke": "worker_qualification_smoke",
        "faults": "worker_qualification_fault",
        "replay": "worker_qualification_gate",
        "load": "worker_qualification_gate",
        "retention": "worker_qualification_gate",
        "observe": "worker_qualification_gate",
        "promotion": "worker_qualification",
    }[args.mode]
    test_filter = {
        "replay": "recorded_replay_gate",
        "load": "sustained_load_gate",
        "retention": "retention_transaction_gate",
        "observe": "observation_gate",
    }.get(args.mode)
    # The load gate covers both runtimes in one case, so it is opt-in by count
    # rather than by marker: promotion runs it, every other mode skips it.
    load_count = args.load_count if args.mode in {"load", "promotion"} else 0

    environment = os.environ.copy()
    environment.update(
        {
            "RUN_WORKER_QUALIFICATION": "1",
            "QUALIFICATION_BUILD": "1" if args.build else "0",
            "QUALIFICATION_WORKER": args.worker,
            "QUALIFICATION_REPLAY_COUNT": str(args.replay_count),
            "QUALIFICATION_LOAD_COUNT": str(load_count),
            "QUALIFICATION_LOAD_RATE": str(args.load_rate),
            "QUALIFICATION_LOAD_P95_TOLERANCE": str(args.load_tolerance),
            "QUALIFICATION_OBSERVE_SECONDS": str(args.observe_hours * 3600),
            "QUALIFICATION_RUN_RETENTION": (
                "1" if args.worker == "storage" and args.mode in {"retention", "promotion"} else "0"
            ),
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-ra",
        "tests/worker_qualification",
        "-m",
        marker,
    ]
    if test_filter is not None:
        command.extend(("-k", test_filter))
    completed = subprocess.run(  # nosec B603
        command,
        cwd=ROOT,
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
