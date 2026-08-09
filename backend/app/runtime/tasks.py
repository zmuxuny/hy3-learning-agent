from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


_tasks: dict[str, asyncio.Task] = {}


def start_tracked_task(run_id: str, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Start one in-process task per Run and make it cancellable by Run id."""
    previous = _tasks.get(run_id)
    if previous and not previous.done():
        coroutine.close()
        return previous

    task = asyncio.create_task(coroutine)
    _tasks[run_id] = task

    def cleanup(completed: asyncio.Task) -> None:
        if _tasks.get(run_id) is completed:
            _tasks.pop(run_id, None)

    task.add_done_callback(cleanup)
    return task


def cancel_tracked_task(run_id: str) -> bool:
    task = _tasks.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True
