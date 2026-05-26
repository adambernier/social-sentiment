"""Request-shaping helpers for the per-symbol fan-out producers.

A plain ``asyncio.gather`` over every tracked symbol fires N requests at once
each poll cycle. That burst is what trips external rate limiters (StockTwits,
Finnhub) as the symbol list grows — even when the *average* request rate is
well under quota. These helpers convert that burst into a paced, concurrency-
bounded trickle without changing the average throughput, so adding symbols
degrades freshness gracefully instead of cliff-diving into exponential backoff.

Dependency-free on purpose (asyncio only), so it works in the baked producer
images without touching any requirements file.
"""

import asyncio
import time
from typing import Awaitable, Callable, Optional, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class AsyncRateLimiter:
    """Token-bucket rate limiter for a single asyncio event loop.

    Permits at most ``max_rate`` acquisitions per ``period`` seconds, refilling
    continuously so requests are spread out rather than fired all at once.
    Tokens accumulate up to ``max_rate`` while idle, so a quiet limiter still
    lets a short burst through before pacing kicks in.

    Not safe to share across event loops/threads; create one per producer loop.
    """

    def __init__(self, max_rate: float, period: float = 60.0):
        self.max_rate = max(float(max_rate), 1e-9)
        self.period = max(float(period), 1e-9)
        self._tokens = self.max_rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        # Hold the lock across the wait so acquisitions are handed out one at a
        # time, evenly spaced — this is what turns a burst into a trickle. When
        # tokens are available (the common case at small N) the lock is held
        # only momentarily, so callers proceed without added latency.
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.max_rate,
                    self._tokens + (now - self._last) * (self.max_rate / self.period),
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                await asyncio.sleep(deficit * (self.period / self.max_rate))


async def paced_gather(
    items: Sequence[T],
    fetch_fn: Callable[[T], Awaitable[R]],
    *,
    max_concurrency: int = 4,
    limiter: Optional[AsyncRateLimiter] = None,
) -> list[R]:
    """Run ``fetch_fn`` over ``items`` like ``asyncio.gather``, but shaped.

    Two independent throttles smooth the request burst a plain ``gather`` would
    create:

    * ``max_concurrency`` bounds how many calls are in flight at once.
    * ``limiter`` (optional) paces how often a new call may *start*.

    The rate token is taken only after a concurrency slot is held, so a token
    is never "spent" while a coroutine is stuck waiting for a slot — every token
    maps 1:1 to an actual request start. Results are returned in the same order
    as ``items`` (matching ``asyncio.gather``).
    """
    sem = asyncio.Semaphore(max(int(max_concurrency), 1))

    async def _run(item: T) -> R:
        async with sem:
            if limiter is not None:
                await limiter.acquire()
            return await fetch_fn(item)

    return await asyncio.gather(*(_run(item) for item in items))
