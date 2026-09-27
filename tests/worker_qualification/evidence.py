"""Release evidence records for the worker qualification gates.

An artifact is only useful as promotion evidence if it names the candidate it
exercised. Every gate record therefore carries the source revision, the
container images, and the host that ran it, alongside the gate's own outcome, so
a promotion record can cite the exact code and image that passed instead of
trusting a file's modification time.

A gate removes its artifact when it starts and writes one when it finishes,
whether it passed or failed. Absence therefore means the run never completed,
and presence always states which of the two happened.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import socket
import subprocess  # nosec B404 - fixed git command vector only
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

_ARTIFACT_STEM = {
    "observation": "worker-observation",
    "replay": "worker-replay",
    "load": "worker-load",
}
_ARTIFACT_ENV = {
    "observation": "QUALIFICATION_OBSERVE_ARTIFACT",
    "replay": "QUALIFICATION_REPLAY_ARTIFACT",
    "load": "QUALIFICATION_LOAD_ARTIFACT",
}


def artifact_path(kind: str, worker: str) -> Path:
    """Resolve one gate's evidence path, honouring its development override."""
    if kind not in _ARTIFACT_STEM:
        raise ValueError(f"unknown evidence artifact kind: {kind}")
    override = os.getenv(_ARTIFACT_ENV[kind])
    if override:
        return Path(override)
    return Path("artifacts") / f"{_ARTIFACT_STEM[kind]}-{worker}.json"


def git_revision() -> dict[str, Any]:
    """Source revision under test; `dirty` flags uncommitted working-tree edits."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "commit": commit,
        "dirty": None if status is None else bool(status),
    }


def host_platform() -> dict[str, str]:
    """Identify the machine that ran the gate."""
    return {"hostname": socket.gethostname(), "platform": platform.platform()}


@contextlib.contextmanager
def evidence_artifact(
    path: Path,
    record: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Publish one gate's JSON record, including when the gate fails.

    The caller enriches the yielded mapping as the gate progresses; this helper
    stamps the outcome and the finish time on the way out. Failed records are
    retained deliberately, so a missing artifact means the gate never finished
    rather than that it failed quietly.
    """
    path.unlink(missing_ok=True)
    record.setdefault("started_at", time.time())
    try:
        yield record
    except BaseException as error:  # noqa: BLE001 - the record must survive any exit
        record.update(
            {
                "status": "failed",
                "finished_at": time.time(),
                "error": f"{type(error).__name__}: {error}"[:2000],
            }
        )
        _write(path, record)
        raise
    else:
        record.update({"status": "passed", "finished_at": time.time()})
        _write(path, record)


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(  # nosec B603
            ["git", *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _write(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(record), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
