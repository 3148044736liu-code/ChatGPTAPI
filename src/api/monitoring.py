"""In-memory request telemetry for the operations dashboard.

The tracker intentionally stores metadata only: caller address, optional task
labels, endpoint, status, and timing. Request/response bodies and credentials
are never retained.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

from src.config import Config
from src.log import setup_logging


access_log = setup_logging("api_access")
error_log = setup_logging("api_errors")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _header(headers: dict[bytes, bytes], name: bytes) -> str:
    return headers.get(name, b"").decode("utf-8", errors="replace").strip()


class RequestTracker:
    """Track active/recent API callers for the current server process."""

    EXCLUDED_PREFIXES = ("/dashboard", "/api/dashboard")
    EXCLUDED_PATHS = {"/docs", "/redoc", "/openapi.json", "/favicon.ico"}

    def __init__(self) -> None:
        self.started_at = time.time()
        self.total_requests = 0
        self.active_requests = 0
        self.success_requests = 0
        self.error_requests = 0
        self.path_counts: Counter[str] = Counter()
        self.status_counts: Counter[int] = Counter()
        self.clients: dict[str, dict[str, Any]] = {}
        self.recent: deque[dict[str, Any]] = deque(
            maxlen=Config.DASHBOARD_MAX_RECENT_REQUESTS
        )

    @classmethod
    def should_track(cls, path: str) -> bool:
        return path not in cls.EXCLUDED_PATHS and not path.startswith(
            cls.EXCLUDED_PREFIXES
        )

    def begin(self, scope: dict[str, Any]) -> dict[str, Any] | None:
        path = str(scope.get("path", ""))
        if not self.should_track(path):
            return None

        now = time.time()
        headers = dict(scope.get("headers", []))
        client = scope.get("client") or ("unknown", 0)
        ip_address = str(client[0] or "unknown")
        machine_name = (
            _header(headers, b"x-task-machine")
            or _header(headers, b"x-machine-name")
            or _header(headers, b"x-client-name")
        )[:120]
        task_name = _header(headers, b"x-task-name")[:120]
        user_agent = _header(headers, b"user-agent")[:240]
        request_id = _header(headers, b"x-request-id")[:80] or uuid.uuid4().hex
        key = f"{ip_address}|{machine_name or '-'}"

        item = self.clients.get(key)
        if item is None:
            item = {
                "key": key,
                "ip_address": ip_address,
                "machine_name": machine_name,
                "task_names": set(),
                "owners": set(),
                "user_agents": set(),
                "first_seen": now,
                "last_seen": now,
                "active_requests": 0,
                "total_requests": 0,
                "success_requests": 0,
                "error_requests": 0,
                "last_method": "",
                "last_path": "",
                "last_status": None,
                "last_duration_ms": None,
            }
            self.clients[key] = item

        if task_name:
            item["task_names"].add(task_name)
        if user_agent:
            item["user_agents"].add(user_agent)
        item["last_seen"] = now
        item["active_requests"] += 1
        item["total_requests"] += 1
        item["last_method"] = str(scope.get("method", ""))
        item["last_path"] = path

        self.total_requests += 1
        self.active_requests += 1
        self.path_counts[path] += 1

        return {
            "key": key,
            "started_at": now,
            "method": str(scope.get("method", "")),
            "path": path,
            "query": scope.get("query_string", b"").decode(
                "utf-8", errors="replace"
            )[:300],
            "ip_address": ip_address,
            "machine_name": machine_name,
            "task_name": task_name,
            "user_agent": user_agent,
            "request_id": request_id,
        }

    def finish(
        self,
        context: dict[str, Any] | None,
        scope: dict[str, Any],
        status_code: int,
        owner: str = "",
    ) -> None:
        if context is None:
            return

        now = time.time()
        elapsed_ms = max(0, int((now - context["started_at"]) * 1000))
        item = self.clients.get(context["key"])
        owner = owner or str(scope.get("catgpt.owner_id", ""))
        if item is not None:
            item["active_requests"] = max(0, item["active_requests"] - 1)
            item["last_seen"] = now
            item["last_status"] = status_code
            item["last_duration_ms"] = elapsed_ms
            if owner:
                item["owners"].add(owner)
            if status_code < 400:
                item["success_requests"] += 1
            else:
                item["error_requests"] += 1

        self.active_requests = max(0, self.active_requests - 1)
        self.status_counts[status_code] += 1
        if status_code < 400:
            self.success_requests += 1
        else:
            self.error_requests += 1

        self.recent.appendleft(
            {
                "timestamp": _iso(now),
                "request_id": context["request_id"],
                "ip_address": context["ip_address"],
                "machine_name": context["machine_name"],
                "task_name": context["task_name"],
                "owner": owner,
                "method": context["method"],
                "path": context["path"],
                "status": status_code,
                "duration_ms": elapsed_ms,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        online_window = Config.DASHBOARD_ONLINE_WINDOW_SECONDS
        clients: list[dict[str, Any]] = []

        for item in self.clients.values():
            age = max(0, int(now - item["last_seen"]))
            online = item["active_requests"] > 0 or age <= online_window
            clients.append(
                {
                    "ip_address": item["ip_address"],
                    "machine_name": item["machine_name"],
                    "display_name": item["machine_name"]
                    or f"任务机 {item['ip_address']}",
                    "task_names": sorted(item["task_names"]),
                    "owners": sorted(item["owners"]),
                    "user_agents": sorted(item["user_agents"])[:3],
                    "first_seen": _iso(item["first_seen"]),
                    "last_seen": _iso(item["last_seen"]),
                    "last_seen_seconds_ago": age,
                    "online": online,
                    "active_requests": item["active_requests"],
                    "total_requests": item["total_requests"],
                    "success_requests": item["success_requests"],
                    "error_requests": item["error_requests"],
                    "last_method": item["last_method"],
                    "last_path": item["last_path"],
                    "last_status": item["last_status"],
                    "last_duration_ms": item["last_duration_ms"],
                }
            )

        clients.sort(
            key=lambda row: (
                not row["online"],
                -row["active_requests"],
                row["last_seen_seconds_ago"],
            )
        )
        online_count = sum(1 for item in clients if item["online"])
        success_rate = (
            round(self.success_requests * 100 / self.total_requests, 1)
            if self.total_requests
            else 100.0
        )

        return {
            "started_at": _iso(self.started_at),
            "uptime_seconds": int(now - self.started_at),
            "total_requests": self.total_requests,
            "active_requests": self.active_requests,
            "success_requests": self.success_requests,
            "error_requests": self.error_requests,
            "success_rate": success_rate,
            "unique_clients": len(clients),
            "online_clients": online_count,
            "online_window_seconds": online_window,
            "clients": clients,
            "recent_requests": list(self.recent),
            "top_endpoints": [
                {"path": path, "count": count}
                for path, count in self.path_counts.most_common(10)
            ],
            "status_counts": [
                {"status": status, "count": count}
                for status, count in sorted(self.status_counts.items())
            ],
        }


class RequestTelemetryMiddleware:
    """ASGI middleware that records response status and elapsed time."""

    def __init__(self, app, tracker: RequestTracker) -> None:
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        context = self.tracker.begin(scope)
        status_code = 500
        owner_id = ""
        failure: Exception | None = None

        async def send_wrapper(message):
            nonlocal status_code, owner_id
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                owner_id = str(scope.get("catgpt.owner_id", ""))
                if context is not None:
                    headers = list(message.get("headers", []))
                    if not any(name.lower() == b"x-request-id" for name, _ in headers):
                        headers.append(
                            (b"x-request-id", context["request_id"].encode("ascii"))
                        )
                        message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            failure = exc
            status_code = 500
            if context is not None:
                error_log.exception(
                    "Unhandled request exception | request_id=%s | ip=%s | %s %s",
                    context["request_id"],
                    context["ip_address"],
                    context["method"],
                    context["path"],
                )
            raise
        finally:
            self.tracker.finish(context, scope, status_code, owner_id)
            if context is not None:
                elapsed_ms = max(
                    0, int((time.time() - context["started_at"]) * 1000)
                )
                owner_id = owner_id or str(scope.get("catgpt.owner_id", ""))
                record = {
                    "request_id": context["request_id"],
                    "ip": context["ip_address"],
                    "machine": context["machine_name"],
                    "task": context["task_name"],
                    "owner": owner_id,
                    "method": context["method"],
                    "path": context["path"],
                    "status": status_code,
                    "duration_ms": elapsed_ms,
                    "user_agent": context["user_agent"],
                    "exception": type(failure).__name__ if failure else "",
                }
                log_method = access_log.error if status_code >= 500 else (
                    access_log.warning if status_code >= 400 else access_log.info
                )
                log_method(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


request_tracker = RequestTracker()
