"""Fixtures for opt-in Docker worker qualification tests."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .harness import QualificationStack

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "worker_qualification.json"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Limit a manual promotion run to its current worker stage."""
    selected = os.getenv("QUALIFICATION_WORKER")
    if selected not in {"preprocessing", "sentiment", "storage"}:
        return
    skip = pytest.mark.skip(reason=f"qualification is limited to {selected}")
    for item in items:
        callspec = getattr(item, "callspec", None)
        item_worker = callspec.params.get("worker") if callspec is not None else None
        if item_worker is not None and item_worker != selected or item_worker is None and "storage" in item.name and selected != "storage":
            item.add_marker(skip)


@pytest.fixture(scope="session")
def qualification_fixtures() -> dict[str, dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture(scope="session")
def qualification_stack() -> Iterator[QualificationStack]:
    if os.getenv("RUN_WORKER_QUALIFICATION") != "1":
        pytest.skip("set RUN_WORKER_QUALIFICATION=1 to start the Docker harness")
    stack = QualificationStack()
    stack.start(build=os.getenv("QUALIFICATION_BUILD", "0") == "1")
    try:
        yield stack
    finally:
        stack.close()
