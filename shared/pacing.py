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


class PerSymbolBackoff:
    """Tracks an independent exponential backoff per symbol.

    A symbol that reports rate-limiting is put into a cooldown that doubles on
    each consecutive hit (capped at ``max_backoff``); a symbol that succeeds
    resets immediately. Each poll cycle, call :meth:`due` to pick the symbols
    eligible to poll, then feed every result back via :meth:`record`. This
    decouples one throttled ticker from the rest of the batch — the others keep
    polling on cadence instead of all sharing a single global backoff.

    Cooldowns are measured against ``time.monotonic``; ``base_interval`` is the
    first penalty (one poll cycle is the natural unit), so a penalty of N means
    roughly "skip the next N/base cycles".
    """

    def __init__(self, base_interval: float, max_backoff: float):
        self.base = max(float(base_interval), 1e-9)
        self.max_backoff = max(float(max_backoff), self.base)
        self._penalty: dict[str, float] = {}  # current cooldown length, seconds
        self._next_ok: dict[str, float] = {}  # monotonic time eligible again

    def due(self, symbols: Sequence[str], now: Optional[float] = None) -> list[str]:
        """Return the subset of ``symbols`` not currently in cooldown."""
        t = time.monotonic() if now is None else now
        return [s for s in symbols if t >= self._next_ok.get(s, 0.0)]

    def record(self, symbol: str, rate_limited: bool, now: Optional[float] = None) -> None:
        """Update one symbol's state from its latest poll outcome."""
        if not rate_limited:
            self._penalty.pop(symbol, None)
            self._next_ok.pop(symbol, None)
            return
        t = time.monotonic() if now is None else now
        prev = self._penalty.get(symbol, 0.0)
        nxt = min(self.max_backoff, prev * 2 if prev > 0 else self.base)
        self._penalty[symbol] = nxt
        self._next_ok[symbol] = t + nxt

    def penalized(self) -> list[str]:
        """Symbols currently carrying a cooldown (for logging/metrics)."""
        return sorted(self._penalty)
