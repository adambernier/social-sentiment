"""Explicit outcomes shared by external polling producers."""

from dataclasses import dataclass
from enum import Enum


class PollStatus(str, Enum):
    SUCCESS = "success"
    NO_DATA = "no_data"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    TRANSIENT_ERROR = "transient_error"
    ERROR = "error"


@dataclass(frozen=True)
class PollOutcome:
    status: PollStatus
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.retry_after_seconds is not None:
            if self.status is not PollStatus.RATE_LIMITED:
                raise ValueError("retry_after_seconds requires a rate-limited outcome")
            if self.retry_after_seconds <= 0:
                raise ValueError("retry_after_seconds must be positive")
