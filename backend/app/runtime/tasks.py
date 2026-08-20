from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


_tasks: dict[str, asyncio.Task] = {}
_pending_wakes: dict[str, tuple[str, Coroutine[Any, Any, Any]]] = {}
_active_wake_keys: dict[str, str] = {}


def _close_coroutine(coroutine: Coroutine[Any, Any, Any]) -> None:
    coroutine.close()


def _launch_tracked_task(
    run_id: str,
    coroutine: Coroutine[Any, Any, Any],
    *,
    wake_key: str | None = None,
) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _tasks[run_id] = task
    if wake_key is None:
        _active_wake_keys.pop(run_id, None)
    else:
        _active_wake_keys[run_id] = wake_key

    def cleanup(completed: asyncio.Task) -> None:
        # A newer task may already have replaced this one between completion
        # and callback delivery.  Only the task that still owns the slot may
        # hand it off or remove it.
        if _tasks.get(run_id) is not completed:
            return
        _tasks.pop(run_id, None)
        if wake_key is not None and _active_wake_keys.get(run_id) == wake_key:
            _active_wake_keys.pop(run_id, None)
        pending = _pending_wakes.pop(run_id, None)
        if pending is not None:
            pending_key, pending_coroutine = pending
            _launch_tracked_task(
                run_id,
                pending_coroutine,
                wake_key=pending_key,
            )

    task.add_done_callback(cleanup)
    return task


def start_tracked_task(run_id: str, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Start one in-process task per Run and make it cancellable by Run id."""
    previous = _tasks.get(run_id)
    if previous and not previous.done():
        _close_coroutine(coroutine)
        return previous
    pending = _pending_wakes.pop(run_id, None)
    if pending is not None:
        _close_coroutine(coroutine)
        pending_key, pending_coroutine = pending
        return _launch_tracked_task(
            run_id,
            pending_coroutine,
            wake_key=pending_key,
        )
    return _launch_tracked_task(run_id, coroutine)


def wake_tracked_task(
    run_id: str,
    coroutine: Coroutine[Any, Any, Any],
    *,
    wake_key: str,
) -> asyncio.Task:
    """Ensure one task observes a durable wake without racing old-task cleanup.

    A Runtime may commit ``waiting_approval`` immediately before its in-process
    task returns.  If the approval endpoint races that return, a normal start
    sees the old task and discards the resume coroutine.  Keep one successor in
    the task slot instead; the old owner's cleanup deterministically hands off
    to it.  The stable wake key coalesces duplicate delivery both while the
    successor is pending and while it is running.
    """

    if not wake_key:
        _close_coroutine(coroutine)
        raise ValueError("wake_key must be non-empty")
    previous = _tasks.get(run_id)
    if (
        previous is not None
        and not previous.done()
        and _active_wake_keys.get(run_id) == wake_key
    ):
        _close_coroutine(coroutine)
        return previous
    pending = _pending_wakes.get(run_id)
    if pending is not None:
        # One pending Runtime is sufficient: it will claim the latest durable
        # database state, so an additional wake must not create a second worker.
        _close_coroutine(coroutine)
        return previous if previous is not None else _launch_pending_wake(run_id)
    if previous is not None and not previous.done():
        _pending_wakes[run_id] = (wake_key, coroutine)
        return previous
    return _launch_tracked_task(run_id, coroutine, wake_key=wake_key)


def _launch_pending_wake(run_id: str) -> asyncio.Task:
    wake_key, coroutine = _pending_wakes.pop(run_id)
    return _launch_tracked_task(run_id, coroutine, wake_key=wake_key)


def cancel_tracked_task(run_id: str) -> bool:
    pending = _pending_wakes.pop(run_id, None)
    if pending is not None:
        _close_coroutine(pending[1])
    _active_wake_keys.pop(run_id, None)
    task = _tasks.get(run_id)
    if task is None or task.done():
        return pending is not None
    task.cancel()
    return True


async def cancel_and_wait_tracked_task(run_id: str) -> bool:
    """Cancel one tracked task and wait until its cleanup has finished.

    Terminal state is committed before callers use this helper. Waiting here
    prevents a cancelled worker from retaining a SQLite connection or issuing
    a late write after the cancellation API (or parent cleanup) has returned.
    The existing non-blocking helper remains available to synchronous wakeup
    paths that only need to signal cancellation.
    """

    task = _tasks.get(run_id)
    cancelled = cancel_tracked_task(run_id)
    if task is not None and task is not asyncio.current_task():
        await asyncio.gather(task, return_exceptions=True)
    return cancelled
