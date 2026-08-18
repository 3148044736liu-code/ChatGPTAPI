"""Background task that periodically removes expired managed files.

Expired file rows (and the bytes they reference on disk) were previously
only cleaned up once, at server startup. This module runs the same sweep
on a fixed interval while the server is up, so long-running deployments
do not keep accumulating stale files.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.config import Config
from src.log import setup_logging
from src.storage.database import Database

log = setup_logging("file_cleanup")


def sweep_expired_files(database: Database) -> int:
    """Delete all expired file records and their on-disk bytes.

    Returns the number of records removed. Safe to call from any context
    (synchronous SQLite access guarded by the Database lock).
    """
    removed = 0
    for expired in database.expired_files():
        try:
            path = Path(expired["stored_path"])
            if path.is_file():
                path.unlink(missing_ok=True)
            # Best-effort: remove now-empty owner/key directories so we do
            # not leave behind a tree of empty folders.
            parent = path.parent
            for _ in range(2):
                if parent != Config.FILES_DIR and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
        except OSError as error:
            database.set_file_status(
                expired["id"], expired["owner_id"], "DELETE_PENDING", str(error)[:500]
            )
            log.warning("Expired file deletion deferred for %s: %s", expired["id"], error)
            continue
        database.delete_file(expired["id"], expired["owner_id"])
        removed += 1
    if removed:
        log.info(f"Cleaned up {removed} expired file(s)")
    return removed


def sweep_temp_files(max_age_seconds: int = 3600) -> int:
    """Remove abandoned bounded-upload .part files from the controlled temp root."""
    removed = 0
    cutoff = time.time() - max_age_seconds
    Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for path in Config.TEMP_DIR.glob("*.part"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as error:
            log.debug("Could not remove temporary file %s: %s", path.name, error)
    return removed


class FileCleanupTask:
    """Async loop that sweeps expired files every configured interval."""

    def __init__(self, database: Database, interval_minutes: int | None = None) -> None:
        self._database = database
        self._interval = (interval_minutes or Config.FILE_CLEANUP_INTERVAL_MINUTES) * 60
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                sweep_expired_files(self._database)
                sweep_temp_files()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # never let the loop die
                log.warning(f"File cleanup sweep failed: {error}")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="file-cleanup")
            log.info(
                f"Background file cleanup started "
                f"(interval={self._interval // 60} min)"
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
