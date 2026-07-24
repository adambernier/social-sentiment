import asyncio

import pytest

from shared.runtime import supervise_long_running


@pytest.mark.asyncio
async def test_supervisor_cancels_siblings_and_surfaces_worker_failure():
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def failing_worker():
        await sibling_started.wait()
        raise RuntimeError("worker crashed")

    async def sibling():
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    with pytest.raises(ExceptionGroup) as exc_info:
        await supervise_long_running(
            ("failing-worker", failing_worker),
            ("sibling", sibling),
        )

    assert any(
        isinstance(error, RuntimeError) and str(error) == "worker crashed"
        for error in exc_info.value.exceptions
    )
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_supervisor_treats_normal_long_running_task_exit_as_failure():
    sibling_cancelled = asyncio.Event()

    async def stopped_worker():
        return

    async def sibling():
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    with pytest.raises(ExceptionGroup) as exc_info:
        await supervise_long_running(
            ("stopped-worker", stopped_worker),
            ("sibling", sibling),
        )

    assert any(
        isinstance(error, RuntimeError)
        and str(error) == "stopped-worker stopped unexpectedly"
        for error in exc_info.value.exceptions
    )
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_supervisor_propagates_cancellation_and_cleans_up_all_tasks():
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def wait_forever(started, cancelled):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor = asyncio.create_task(
        supervise_long_running(
            (
                "first",
                lambda: wait_forever(first_started, first_cancelled),
            ),
            (
                "second",
                lambda: wait_forever(second_started, second_cancelled),
            ),
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=0.5)
    await asyncio.wait_for(second_started.wait(), timeout=0.5)
    supervisor.cancel()

    with pytest.raises(asyncio.CancelledError):
        await supervisor

    assert first_cancelled.is_set()
    assert second_cancelled.is_set()
