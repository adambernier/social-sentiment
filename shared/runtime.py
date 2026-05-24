"""Shared process runtime helpers for graceful shutdown.

Services run a long-lived async ``main()`` (or, for the market producer, a
blocking ``main()``) and historically handled only ``KeyboardInterrupt``.
Containers stop processes with ``SIGTERM``, which otherwise kills the process
mid-batch and skips the ``finally`` blocks that requeue in-flight messages.
These helpers install ``SIGINT``/``SIGTERM`` handlers so shutdown is clean.
"""

import asyncio
import logging
import signal

logger = logging.getLogger("runtime")


def run(main, *, name: str = "service") -> None:
    """Run an async ``main`` coroutine function with graceful shutdown.

    On SIGINT/SIGTERM the main task is cancelled, allowing its ``finally``
    blocks (message requeue, connection close) to run, then the resulting
    ``CancelledError`` is swallowed and a clean shutdown line is logged.
    """

    async def _runner() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(main())

        def _request_shutdown() -> None:
            if not task.done():
                logger.info("Shutdown signal received, stopping %s...", name)
                task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except NotImplementedError:
                pass  # add_signal_handler unavailable on this platform (e.g. Windows)

        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        pass  # fallback if signal handlers could not be installed
    logger.info("%s stopped.", name)


def install_sigterm_handler() -> None:
    """For blocking (non-async) services: translate ``SIGTERM`` into
    ``KeyboardInterrupt`` so an existing ``except KeyboardInterrupt`` handles it.
    """

    def _handler(signum, frame):
        raise KeyboardInterrupt()

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass  # not running in the main thread / not supported
