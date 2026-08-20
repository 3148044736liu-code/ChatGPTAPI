"""Operations dashboard routes and runtime status aggregation."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from src.api.monitoring import request_tracker
from src.api.runtime_control import runtime_controller
from src.api.risk_controller import risk_controller
from src.config import Config
from src.log import setup_logging

if TYPE_CHECKING:
    from src.api.session_service import SessionWorkerPool
    from src.browser.manager import BrowserManager
    from src.storage.database import Database


dashboard_router = APIRouter(tags=["运维仪表盘"])
_dashboard_file = Path(__file__).with_name("static") / "dashboard.html"
_developer_guide_file = Config.PROJECT_ROOT / "dos" / "GPT-FastAPI_API_Usage_Guide.md"

_browser: BrowserManager | None = None
_pool: SessionWorkerPool | None = None
_database: Database | None = None
_login_check_lock = asyncio.Lock()
_login_cache: dict[str, Any] = {"value": False, "checked_at": 0.0}
_log_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+\.log$")
_project_id_pattern = re.compile(r"^[a-zA-Z0-9_.-]{2,80}$")
audit_log = setup_logging("project_audit")


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    project_id: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=500)
    multi_agent: bool = False


class ProjectStatusRequest(BaseModel):
    enabled: bool


def set_dashboard_services(browser, pool, database) -> None:
    global _browser, _pool, _database
    _browser = browser
    _pool = pool
    _database = database
    _login_cache.update(value=False, checked_at=0.0)


def _foreground_state() -> dict[str, Any]:
    path = Config.BROWSER_FOREGROUND_STATE_FILE
    fallback: dict[str, Any] = {
        "enabled": Config.BROWSER_KEEP_FOREGROUND,
        "active": False,
        "visible_windows": 0,
        "chrome_processes": 0,
        "topmost_enforced": False,
        "last_enforced_at": None,
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        age_seconds = max(0, int(time.time() - path.stat().st_mtime))
        fallback.update(payload)
        fallback["age_seconds"] = age_seconds
        fallback["active"] = bool(payload.get("running")) and age_seconds <= 15
    except (OSError, ValueError, TypeError):
        fallback["age_seconds"] = None
    return fallback


def _browser_state() -> dict[str, Any]:
    result = {
        "ready": False,
        "main_page_open": False,
        "page_count": 0,
        "current_url": "",
        "provider": Config.PROVIDER,
    }
    if _browser is None:
        return result
    try:
        page = _browser.page
        result["main_page_open"] = not page.is_closed()
        result["current_url"] = page.url if result["main_page_open"] else ""
        result["page_count"] = sum(
            1 for item in _browser.context.pages if not item.is_closed()
        )
        result["ready"] = result["main_page_open"] and result["page_count"] > 0
    except Exception:
        pass
    return result


async def _logged_in(browser_ready: bool) -> bool:
    """Return a short-lived real login check without polling the DOM at 5 Hz."""
    if not browser_ready or _browser is None:
        _login_cache.update(value=False, checked_at=time.time())
        return False
    now = time.time()
    if now - float(_login_cache["checked_at"]) < 60:
        return bool(_login_cache["value"])
    async with _login_check_lock:
        now = time.time()
        if now - float(_login_cache["checked_at"]) < 60:
            return bool(_login_cache["value"])
        try:
            value = await asyncio.wait_for(_browser.is_logged_in(), timeout=20)
        except Exception:
            value = False
        _login_cache.update(value=bool(value), checked_at=time.time())
        return bool(value)


@dashboard_router.get("/dashboard", include_in_schema=False)
@dashboard_router.get("/dashboard/", include_in_schema=False)
async def dashboard_page():
    return HTMLResponse(
        _dashboard_file.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@dashboard_router.get("/api/dashboard/developer-guide", include_in_schema=False)
async def download_developer_guide(request: Request):
    """Download the current integration and development guide as Markdown."""
    _require_admin(request)
    if not _developer_guide_file.is_file():
        raise HTTPException(status_code=404, detail="Developer guide not found")
    return FileResponse(
        path=_developer_guide_file,
        media_type="text/markdown; charset=utf-8",
        filename="GPT-FastAPI_Developer_Guide.md",
        headers={"Cache-Control": "no-store"},
    )


def _log_files() -> list[dict[str, Any]]:
    """Return daily log files without exposing arbitrary filesystem paths."""
    files: list[dict[str, Any]] = []
    try:
        paths = sorted(
            Config.LOG_DIR.glob("*.log"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return files
    for path in paths[:100]:
        try:
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                }
            )
        except OSError:
            continue
    return files


def _tail_log(path: Path, max_lines: int) -> list[str]:
    """Read the last N UTF-8 lines while bounding memory usage."""
    max_bytes = 2 * 1024 * 1024
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        payload = handle.read()
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines[-max_lines:]


@dashboard_router.get(
    "/api/dashboard/logs",
    summary="查看服务日志",
    description="列出日志文件，或返回指定日志文件的末尾内容。",
)
async def dashboard_logs(request: Request, file: str = "", lines: int = 200):
    _require_admin(request)
    files = _log_files()
    if not file:
        preferred = next(
            (item["name"] for item in files if item["name"].startswith("api_access_")),
            files[0]["name"] if files else "",
        )
        file = preferred
    if not file:
        return {"files": files, "selected": "", "lines": []}
    if not _log_name_pattern.fullmatch(file):
        raise HTTPException(status_code=400, detail="Invalid log filename")
    path = Config.LOG_DIR / file
    if not path.is_file() or path.parent.resolve() != Config.LOG_DIR.resolve():
        raise HTTPException(status_code=404, detail="Log file not found")
    return {
        "files": files,
        "selected": file,
        "lines": _tail_log(path, max(20, min(lines, 1000))),
    }


def _project_database():
    if _database is None:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    return _database


def _require_admin(request: Request) -> None:
    """Require a dedicated admin secret; private origin is defense in depth."""
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
    provided = request.headers.get("x-admin-token", "") or bearer
    expected = Config.ADMIN_TOKEN
    bootstrap_allowed = (
        not expected
        and bool(Config.BOOTSTRAP_ADMIN_TOKEN)
        and request.method == "POST"
        and request.url.path == "/api/dashboard/projects"
        and _database is not None
        and _database.project_count() == 0
    )
    if bootstrap_allowed:
        expected = Config.BOOTSTRAP_ADMIN_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Admin authentication is not configured")
    if not provided or not hmac.compare_digest(provided, expected):
        audit_log.warning(
            "Admin authentication failed | path=%s | operator_ip=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")
    address = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as error:
        raise HTTPException(status_code=403, detail="Project administration is private-network only") from error
    if not (ip.is_private or ip.is_loopback):
        raise HTTPException(status_code=403, detail="Project administration is private-network only")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        origin_host = urlparse(origin).hostname
        request_host = (request.url.hostname or "").lower()
        if not origin_host or origin_host.lower() != request_host:
            raise HTTPException(status_code=403, detail="Cross-site project administration is forbidden")


def _project_id(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    if not candidate:
        candidate = f"project-{secrets.token_hex(4)}"
    if candidate == "default" or not _project_id_pattern.fullmatch(candidate):
        raise HTTPException(
            status_code=422,
            detail="project_id must be 2-80 letters, numbers, dots, underscores or hyphens; 'default' is reserved",
        )
    if candidate in set(Config.token_owners().values()):
        raise HTTPException(status_code=409, detail="project_id conflicts with a legacy .env token owner")
    return candidate


@dashboard_router.get("/api/dashboard/projects", summary="项目 Token 列表")
async def dashboard_projects(request: Request):
    _require_admin(request)
    return {"data": _project_database().list_projects()}


@dashboard_router.post("/api/dashboard/projects", status_code=201, summary="创建项目 Token")
async def create_dashboard_project(payload: ProjectCreateRequest, request: Request):
    _require_admin(request)
    database = _project_database()
    project_id = _project_id(payload.project_id)
    if database.project_exists(project_id):
        raise HTTPException(status_code=409, detail="Project ID already exists")
    project, token = database.create_project(
        project_id=project_id,
        name=payload.name.strip(),
        description=payload.description.strip(),
        multi_agent=payload.multi_agent,
    )
    audit_log.info(
        "Project created | project_id=%s | multi_agent=%s | operator_ip=%s",
        project_id,
        payload.multi_agent,
        request.client.host if request.client else "unknown",
    )
    return {"project": project, "token": token, "token_retrievable": True}


@dashboard_router.get(
    "/api/dashboard/projects/{project_id}/token",
    summary="复制项目 Token",
)
async def get_dashboard_project_token(project_id: str, request: Request):
    _require_admin(request)
    database = _project_database()
    project = database.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        token = database.get_project_token(project_id)
    except Exception as error:
        audit_log.error(
            "Project token decrypt failed | project_id=%s | operator_ip=%s | error=%s",
            project_id,
            request.client.host if request.client else "unknown",
            error,
        )
        raise HTTPException(status_code=500, detail="Token vault decryption failed") from error
    if token is None:
        raise HTTPException(
            status_code=409,
            detail="This legacy project has no encrypted token copy; rotate it once first",
        )
    audit_log.info(
        "Project token copied | project_id=%s | operator_ip=%s",
        project_id,
        request.client.host if request.client else "unknown",
    )
    return {"project_id": project_id, "token": token}


@dashboard_router.post(
    "/api/dashboard/projects/{project_id}/rotate",
    summary="轮换项目 Token",
)
async def rotate_dashboard_project(project_id: str, request: Request):
    _require_admin(request)
    result = _project_database().rotate_project_token(project_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project, token = result
    audit_log.warning(
        "Project token rotated | project_id=%s | operator_ip=%s",
        project_id,
        request.client.host if request.client else "unknown",
    )
    return {"project": project, "token": token, "token_retrievable": True}


@dashboard_router.patch(
    "/api/dashboard/projects/{project_id}",
    summary="启用或停用项目 Token",
)
async def update_dashboard_project(
    project_id: str,
    payload: ProjectStatusRequest,
    request: Request,
):
    _require_admin(request)
    project = _project_database().set_project_enabled(project_id, payload.enabled)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    audit_log.warning(
        "Project status changed | project_id=%s | enabled=%s | operator_ip=%s",
        project_id,
        payload.enabled,
        request.client.host if request.client else "unknown",
    )
    return {"project": project}


@dashboard_router.delete(
    "/api/dashboard/projects/{project_id}",
    summary="删除并吊销项目 Token",
)
async def delete_dashboard_project(project_id: str, request: Request):
    _require_admin(request)
    project = _project_database().delete_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    audit_log.warning(
        "Project token deleted | project_id=%s | operator_ip=%s",
        project_id,
        request.client.host if request.client else "unknown",
    )
    return {
        "deleted": True,
        "project_id": project_id,
        "data_retained": True,
        "message": "Token revoked; existing session and file metadata retained",
    }


@dashboard_router.get("/api/dashboard/runtime", summary="获取浏览器运行层状态")
async def dashboard_runtime(request: Request):
    _require_admin(request)
    return {"runtime": runtime_controller.snapshot()}


@dashboard_router.post("/api/dashboard/runtime/start", summary="启动项目和浏览器")
async def start_dashboard_runtime(request: Request):
    _require_admin(request)
    operator_ip = request.client.host if request.client else "unknown"
    audit_log.warning("Browser runtime start requested | operator_ip=%s", operator_ip)
    try:
        state = await runtime_controller.start()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Browser runtime start failed: {error}") from error
    return {"runtime": state}


@dashboard_router.post("/api/dashboard/runtime/stop", summary="停止项目并关闭浏览器")
async def stop_dashboard_runtime(request: Request):
    _require_admin(request)
    operator_ip = request.client.host if request.client else "unknown"
    audit_log.warning("Browser runtime stop requested | operator_ip=%s", operator_ip)
    try:
        state = await runtime_controller.stop()
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Browser runtime stop failed: {error}") from error
    return {"runtime": state}


@dashboard_router.post("/api/dashboard/runtime/risk/resume", summary="人工恢复 Provider 风控熔断")
async def resume_provider_risk(request: Request):
    _require_admin(request)
    risk_controller.resume()
    audit_log.warning(
        "Provider risk circuit resumed | operator_ip=%s",
        request.client.host if request.client else "unknown",
    )
    return {"risk": risk_controller.snapshot()}


@dashboard_router.get(
    "/api/dashboard/state",
    summary="获取仪表盘状态",
    description="返回服务、浏览器、调用任务机和最近请求的实时状态。",
)
async def dashboard_state(request: Request):
    _require_admin(request)
    telemetry = request_tracker.snapshot()
    pool_state = _pool.snapshot() if _pool else {
        "capacity": 0, "available": 0, "healthy": 0, "busy": 0,
        "queue_depth": 0, "single_page": True, "risk": risk_controller.snapshot(),
    }
    pool_state["healthy"] = _pool.healthy if _pool else 0
    database_state = (
        _database.system_summary()
        if _database is not None
        else {"sessions": 0, "messages": 0, "files": 0, "file_bytes": 0}
    )
    if _database is not None:
        database_state["projects"] = _database.project_count()
        database_state["enabled_projects"] = _database.project_count(enabled_only=True)
    browser_state = _browser_state()
    logged_in = await _logged_in(browser_state["ready"])
    foreground = _foreground_state()

    return {
        "generated_at": time.time(),
        "runtime": runtime_controller.snapshot(),
        "service": {
            "status": "ok",
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "provider": Config.PROVIDER,
            "model": "claude-browser"
            if Config.PROVIDER == "claude"
            else "catgpt-browser",
            "api_host": Config.API_HOST,
            "api_port": Config.API_PORT,
            "base_url": str(request.base_url).rstrip("/"),
            "version": "1.4.4",
            "logged_in": logged_in,
        },
        "browser": browser_state,
        "foreground": foreground,
        "pool": pool_state,
        "storage": database_state,
        "telemetry": telemetry,
    }
