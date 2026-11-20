"""Run isolated Rust worker smoke, fault, retention, or observation gates."""

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
            "Qualify one Rust worker against isolated RabbitMQ/PostgreSQL containers."
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
            "retention",
            "observe",
            "promotion",
        ),
        default="smoke",
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
    if args.observe_hours <= 0:
        raise SystemExit("--observe-hours must be greater than zero")

    marker = {
        "smoke": "worker_qualification_smoke",
        "faults": "worker_qualification_fault",
        "retention": "worker_qualification_gate",
        "observe": "worker_qualification_gate",
        "promotion": "worker_qualification",
    }[args.mode]
    test_filter = {
        "retention": "retention_transaction_gate",
        "observe": "observation_gate",
    }.get(args.mode)

    environment = os.environ.copy()
    environment.update(
        {
            "RUN_WORKER_QUALIFICATION": "1",
            "QUALIFICATION_BUILD": "1" if args.build else "0",
            "QUALIFICATION_WORKER": args.worker,
            "QUALIFICATION_OBSERVE_SECONDS": str(args.observe_hours * 3600),
            "QUALIFICATION_RUN_RETENTION": (
                "1"
                if args.worker == "storage" and args.mode in {"retention", "promotion"}
                else "0"
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
