"""Application logging with automatic daily files and retention cleanup."""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from src.config import Config

_suppress_console = False


class DailyFileHandler(logging.FileHandler):
    """Write to ``<name>_YYYY-MM-DD.log`` and switch at local midnight."""

    def __init__(
        self,
        base_path: Path,
        *,
        retention_days: int,
        encoding: str = "utf-8",
    ) -> None:
        self._directory = base_path.parent
        self._stem = base_path.stem
        self._suffix = base_path.suffix or ".log"
        self._retention_days = max(1, retention_days)
        self._current_date = self._date_string()
        super().__init__(self._dated_path(), encoding=encoding, delay=False)
        self._cleanup_expired_files()

    @staticmethod
    def _date_string() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _dated_path(self) -> Path:
        return self._directory / f"{self._stem}_{self._current_date}{self._suffix}"

    def _rollover_if_needed(self) -> None:
        current_date = self._date_string()
        if current_date == self._current_date:
            return
        self._current_date = current_date
        if self.stream:
            self.stream.flush()
            self.stream.close()
        self.baseFilename = os.path.abspath(self._dated_path())
        self.stream = self._open()
        self._cleanup_expired_files()

    def _cleanup_expired_files(self) -> None:
        cutoff = time.time() - self._retention_days * 86400
        pattern = f"{self._stem}_????-??-??{self._suffix}"
        for path in self._directory.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def emit(self, record: logging.LogRecord) -> None:
        self._rollover_if_needed()
        super().emit(record)


def suppress_console_logs() -> None:
    """Disable all console log handlers (call before any setup_logging)."""
    global _suppress_console
    _suppress_console = True
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            if (
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                and handler.stream in (sys.stdout, sys.stderr)
            ):
                logger.removeHandler(handler)


def setup_logging(
    name: str = "chatgpt_scraper",
    log_file: str | None = None,
) -> logging.Logger:
    """Return a logger backed by an automatically rotated daily file."""
    Config.ensure_dirs()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    base_name = log_file or f"{name}.log"
    file_handler = DailyFileHandler(
        Config.LOG_DIR / base_name,
        retention_days=Config.LOG_RETENTION_DAYS,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if Config.VERBOSE and not _suppress_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger
