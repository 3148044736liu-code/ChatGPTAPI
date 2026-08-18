"""Lifecycle test for the background cleanup task (no real waiting)."""

from __future__ import annotations

import asyncio

from src.files.cleanup import FileCleanupTask


async def test_cleanup_task_start_stop(database):
    task = FileCleanupTask(database, interval_minutes=1)
    task.start()
    assert task._task is not None
    assert not task._task.done()
    await asyncio.sleep(0)  # let the loop start
    await task.stop()
    assert task._task is None


async def test_cleanup_task_double_start_is_safe(database):
    task = FileCleanupTask(database, interval_minutes=1)
    task.start()
    task.start()  # must not spawn a second loop
    await task.stop()
