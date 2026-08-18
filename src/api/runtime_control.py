"""Persistent start/stop control for the browser-backed runtime."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import Config
from src.log import setup_logging

log = setup_logging("runtime_control")

RuntimeCallback = Callable[[], Awaitable[None]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrowserRuntimeController:
    """Own the desired and actual state of the browser runtime."""

    def __init__(self, state_file: Path | None = None) -> None:
        self.state_file = state_file or (Config.DATA_DIR / "browser_runtime_state.json")
        self._lock: asyncio.Lock | None = None
        self._start_callback: RuntimeCallback | None = None
        self._stop_callback: RuntimeCallback | None = None
        self.desired_running = True
        self.status = "stopped"
        self.last_error: str | None = None
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.updated_at = _utc_now()
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
            self.desired_running = bool(payload.get("desired_running", True))
            self.started_at = payload.get("started_at")
            self.stopped_at = payload.get("stopped_at")
        except (OSError, ValueError, TypeError):
            self.desired_running = True

    def _persist(self) -> None:
        Config.ensure_dirs()
        payload = self.snapshot()
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def configure(
        self,
        start_callback: RuntimeCallback,
        stop_callback: RuntimeCallback,
    ) -> None:
        self._start_callback = start_callback
        self._stop_callback = stop_callback

    @property
    def running(self) -> bool:
        return self.status == "running"

    def snapshot(self) -> dict:
        return {
            "desired_running": self.desired_running,
            "running": self.running,
            "status": self.status,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "updated_at": self.updated_at,
        }

    async def start(self) -> dict:
        async with self._get_lock():
            self.desired_running = True
            self.updated_at = _utc_now()
            self._persist()
            if self.running:
                return self.snapshot()
            if self._start_callback is None:
                raise RuntimeError("Browser runtime controller is not configured")
            self.status = "starting"
            self.last_error = None
            self.updated_at = _utc_now()
            self._persist()
            log.info("Browser runtime start requested")
            try:
                await self._start_callback()
            except Exception as error:
                self.status = "error"
                self.last_error = str(error)[:500]
                self.updated_at = _utc_now()
                self._persist()
                log.error("Browser runtime failed to start: %s", error, exc_info=True)
                raise
            self.status = "running"
            self.started_at = _utc_now()
            self.updated_at = self.started_at
            self._persist()
            log.info("Browser runtime started")
            return self.snapshot()

    async def stop(self) -> dict:
        async with self._get_lock():
            # Persist intent first so the external Windows supervisor does not
            # mistake the deliberately closed Chrome process for a crash.
            self.desired_running = False
            self.status = "stopping"
            self.last_error = None
            self.updated_at = _utc_now()
            self._persist()
            log.info("Browser runtime stop requested")
            try:
                if self._stop_callback is not None:
                    await self._stop_callback()
            except Exception as error:
                self.status = "error"
                self.last_error = str(error)[:500]
                self.updated_at = _utc_now()
                self._persist()
                log.error("Browser runtime failed to stop cleanly: %s", error, exc_info=True)
                raise
            self.status = "stopped"
            self.stopped_at = _utc_now()
            self.updated_at = self.stopped_at
            self._persist()
            log.info("Browser runtime stopped")
            return self.snapshot()

    async def restore(self) -> dict:
        """Apply persisted desired state during FastAPI startup."""
        if self.desired_running:
            return await self.start()
        self.status = "stopped"
        self.updated_at = _utc_now()
        self._persist()
        log.info("Browser runtime remains stopped by persisted operator choice")
        return self.snapshot()

    async def shutdown(self) -> None:
        """Close components without changing the operator's desired state."""
        async with self._get_lock():
            if self._stop_callback is not None:
                await self._stop_callback()
            self.status = "stopped"
            self.updated_at = _utc_now()
            self._persist()


runtime_controller = BrowserRuntimeController()


class BrowserRuntimeMiddleware:
    """Reject browser work while stopped; scheduling happens after HTTP admission."""

    _BROWSER_PREFIXES = ("/chat", "/thread", "/threads", "/status")
    _BROWSER_EXACT = {
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/images/generations",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @classmethod
    def _requires_browser(cls, scope: Scope) -> bool:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if path in cls._BROWSER_EXACT:
            return True
        if any(path == prefix or path.startswith(prefix + "/") for prefix in cls._BROWSER_PREFIXES):
            return True
        return method == "POST" and path.startswith("/v1/sessions/") and path.endswith("/messages")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_browser_request = (
            scope.get("type") == "http" and self._requires_browser(scope)
        )
        if is_browser_request and not runtime_controller.running:
            state = runtime_controller.snapshot()
            message = (
                "Browser runtime is stopped. Start it from /dashboard."
                if not state["desired_running"]
                else "Browser runtime is starting or unavailable."
            )
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": message,
                        "type": "runtime_unavailable",
                        "runtime_status": state["status"],
                    }
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
