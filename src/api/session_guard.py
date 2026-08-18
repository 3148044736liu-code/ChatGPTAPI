"""Require and serialize project-scoped browser sessions at the HTTP boundary."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from starlette.responses import JSONResponse


class ProjectSessionMiddleware:
    """Protect project tokens from accidentally using shared browser state.

    For database-backed project tokens, every browser-mutating compatible API
    must include a session_id. Requests for the same project/session are held
    under one lock for their entire lifecycle, including database writes.
    """

    JSON_SESSION_PATHS = {
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/images/generations",
    }
    BLOCKED_SHARED_PREFIXES = ("/chat", "/thread", "/threads", "/status")
    MANAGED_MESSAGE_RE = re.compile(r"^/v1/sessions/([^/]+)/messages$")
    MANAGED_SESSION_RE = re.compile(r"^/v1/sessions/([^/]+)$")

    def __init__(self, app) -> None:
        self.app = app
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    def _replay(body: bytes):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        project: dict[str, Any] = scope.get("catgpt.project") or {}
        path = str(scope.get("path", ""))
        owner = str(scope.get("catgpt.owner_id", "default"))
        project_token = project.get("source") == "project_registry"

        if project_token and path.startswith(self.BLOCKED_SHARED_PREFIXES):
            response = JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "type": "project_session_required",
                        "message": "Project tokens cannot use shared browser routes. Use /v1/sessions and a session_id.",
                    }
                },
            )
            await response(scope, receive, send)
            return

        session_id = ""
        body = b""
        if path in self.JSON_SESSION_PATHS:
            body = await self._read_body(receive)
            receive = self._replay(body)
            try:
                payload = json.loads(body or b"{}")
                session_id = str(payload.get("session_id") or "")
            except (TypeError, ValueError, UnicodeDecodeError):
                session_id = ""
            if project_token and not session_id:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "type": "session_id_required",
                            "message": "Project tokens must include session_id for browser-backed APIs.",
                        }
                    },
                )
                await response(scope, receive, send)
                return
        else:
            match = self.MANAGED_MESSAGE_RE.fullmatch(path)
            if match and scope.get("method") == "POST":
                session_id = match.group(1)
            elif scope.get("method") == "DELETE":
                match = self.MANAGED_SESSION_RE.fullmatch(path)
                if match:
                    session_id = match.group(1)

        if not session_id:
            await self.app(scope, receive, send)
            return

        async with self._lock(f"{owner}:{session_id}"):
            await self.app(scope, receive, send)
