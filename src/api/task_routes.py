"""Task Conversation, request ledger and safe runtime status APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.errors import api_error, provider_error
from src.api.session_service import BrowserQueueFullError, SessionWorkerPool, safe_owner_segment
from src.capabilities import authorize_capability
from src.config import Config
from src.files.service import FileService, FileServiceError
from src.provider_errors import ProviderError, ProviderStateUnknownError
from src.storage.database import Database, StorageLimitError


task_router = APIRouter(tags=["多智能体 Task"])
_database: Database | None = None
_pool: SessionWorkerPool | None = None
_files: FileService | None = None
_background: set[asyncio.Task] = set()


def set_task_services(database: Database | None, pool: SessionWorkerPool | None) -> None:
    global _database, _pool, _files
    _database = database
    _pool = pool
    _files = FileService(database) if database is not None else None


def _db() -> Database:
    if _database is None:
        raise api_error(503, "service_unavailable", "DATABASE_UNAVAILABLE", "Database service is unavailable", retryable=True)
    return _database


def _scheduler() -> SessionWorkerPool:
    if _pool is None:
        raise api_error(503, "runtime_unavailable", "RUNTIME_UNAVAILABLE", "Browser scheduler is unavailable", retryable=True)
    return _pool


def _file_service() -> FileService:
    if _files is None:
        raise api_error(503, "service_unavailable", "FILE_SERVICE_UNAVAILABLE", "File service is unavailable", retryable=True)
    return _files


def _project(request: Request) -> str:
    return str(request.scope.get("catgpt.owner_id", ""))


class TaskCreate(BaseModel):
    agent_id: str = Field(..., min_length=2, max_length=120)
    name: str = Field(default="New task", min_length=1, max_length=200)


class TaskMessage(BaseModel):
    content: str = Field(..., min_length=1, max_length=100_000)
    file_ids: list[str] = Field(default_factory=list, max_items=10)
    idempotency_key: str | None = Field(default=None, max_length=200)


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "project_id": task["project_id"],
        "agent_id": task["agent_id"],
        "session_id": task["session_id"],
        "provider_title": task["provider_title"],
        "name": task["name"],
        "status": task["status"],
        "conversation_ready": bool(task.get("provider_thread_id")),
        "provider_thread_id": task.get("provider_thread_id"),
        "provider_thread_url": task.get("provider_thread_url"),
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "last_started_at": task.get("last_started_at"),
    }


@task_router.post("/v1/tasks", status_code=201, summary="创建独立 Task Conversation")
async def create_task(payload: TaskCreate, request: Request):
    authorize_capability(request, _db(), "task.create", agent_id=payload.agent_id)
    try:
        task = _db().create_task(
            _project(request), payload.agent_id, payload.name.strip(), Config.PROVIDER
        )
    except KeyError as error:
        raise api_error(403, "agent_disabled", "AGENT_DISABLED", "Agent does not exist or is disabled") from error
    return _safe_task(task)


@task_router.get("/v1/tasks", summary="列出 Task Conversations")
async def list_tasks(request: Request, agent_id: str | None = None, limit: int = 100):
    trusted = authorize_capability(request, _db(), "task.list", agent_id=agent_id)
    project = request.scope.get("catgpt.project") or {}
    effective_agent = agent_id
    if project.get("multi_agent") and "agent.read" not in _db().get_agent_capabilities(_project(request), trusted or ""):
        effective_agent = trusted
    return {"data": [_safe_task(item) for item in _db().list_tasks(_project(request), agent_id=effective_agent, limit=max(1, min(limit, 500)))]}


@task_router.get("/v1/agents/{agent_id}/tasks", summary="列出 Agent 的 Tasks")
async def list_agent_tasks(agent_id: str, request: Request):
    trusted = authorize_capability(request, _db(), "task.list", agent_id=agent_id)
    if trusted != agent_id:
        raise api_error(403, "agent_mismatch", "AGENT_MISMATCH", "Agent may only list its own tasks")
    return {"data": [_safe_task(item) for item in _db().list_tasks(_project(request), agent_id=agent_id)]}


@task_router.get("/v1/tasks/{task_id}", summary="查询 Task Conversation")
async def get_task(task_id: str, request: Request):
    authorize_capability(request, _db(), "task.read", task_id=task_id)
    task = _db().get_task(_project(request), task_id)
    if not task:
        raise api_error(404, "task_not_found", "TASK_NOT_FOUND", "Task not found")
    return _safe_task(task)


async def _run_task_request(
    *,
    request_id: str,
    owner: str,
    task: dict[str, Any],
    content: str,
    file_records: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    database = _db()
    database.update_request(request_id, "waiting_gap")
    database.set_task_status(owner, task["task_id"], "queued")
    generated_dir = Config.FILES_DIR / safe_owner_segment(owner) / task["session_id"] / "generated"
    try:
        database.update_request(request_id, "switching_conversation")
        result, generated_paths = await _scheduler().send(
            session_id=task["session_id"],
            provider_thread_id=task.get("provider_thread_id"),
            provider_thread_url=task.get("provider_thread_url"),
            message=content,
            file_paths=[record["stored_path"] for record in file_records],
            generated_dir=generated_dir,
            task_id=task["task_id"],
            request_id=request_id,
        )
        database.update_request(
            request_id,
            "waiting_response",
            provider_thread_id=result.thread_id,
            provider_thread_url=result.thread_url or None,
        )
        if result.thread_id:
            database.activate_session(
                task["session_id"], owner, result.thread_id, result.thread_url or None
            )
        attachments = []
        for path in list(generated_paths) + [Path(image.local_path) for image in result.images or [] if image.local_path]:
            try:
                record = _file_service().register_generated(
                    Path(path), owner_id=owner, session_id=task["session_id"], original_name=Path(path).name
                )
                attachments.append(record)
            except FileServiceError:
                continue
        database.add_message(task["session_id"], "assistant", result.message)
        response = {
            "request_id": request_id,
            "task_id": task["task_id"],
            "session_id": task["session_id"],
            "status": "completed",
            "message": result.message,
            "response_time_ms": result.response_time_ms,
            "attachments": [
                {
                    "id": item["id"], "filename": item["original_name"],
                    "bytes": item["size_bytes"],
                    "download_url": f"{base_url}/v1/files/{item['id']}/content",
                }
                for item in attachments
            ],
        }
        database.update_request(
            request_id,
            "completed",
            provider_thread_id=result.thread_id,
            provider_thread_url=result.thread_url or None,
            result=response,
        )
        database.set_task_status(owner, task["task_id"], "idle", started=True)
        return response
    except BrowserQueueFullError as error:
        database.update_request(request_id, "failed", error_type="queue_full")
        database.set_task_status(owner, task["task_id"], "error")
        raise api_error(429, "queue_full", "QUEUE_FULL", str(error), retryable=True) from error
    except ProviderStateUnknownError as error:
        database.update_request(request_id, "unknown", error_type=error.error_type)
        database.set_task_status(owner, task["task_id"], "unknown")
        raise provider_error(error, request_id=request_id, task_id=task["task_id"])
    except ProviderError as error:
        database.update_request(request_id, "failed", error_type=error.error_type)
        database.set_task_status(owner, task["task_id"], "error")
        raise provider_error(error, request_id=request_id, task_id=task["task_id"])
    except Exception as error:
        database.update_request(request_id, "failed", error_type="provider_error")
        database.set_task_status(owner, task["task_id"], "error")
        raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider request failed", retryable=True, request_id=request_id) from error


def _track_background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background.add(task)

    def _finish(completed: asyncio.Task) -> None:
        _background.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.exception()
        except Exception:
            # The request ledger already contains the sanitized failure state.
            pass

    task.add_done_callback(_finish)


@task_router.post("/v1/tasks/{task_id}/messages", summary="并发提交 Task 消息")
async def send_task_message(task_id: str, payload: TaskMessage, request: Request):
    database = _db()
    owner = _project(request)
    agent_id = authorize_capability(request, database, "task.send", task_id=task_id)
    task = database.get_task(owner, task_id)
    if not task:
        raise api_error(404, "task_not_found", "TASK_NOT_FOUND", "Task not found")
    file_records = []
    if payload.file_ids:
        authorize_capability(request, database, "file.read", task_id=task_id)
    for file_id in payload.file_ids:
        try:
            file_records.append(
                _file_service().resolve_for_session(
                    file_id, owner_id=owner, session_id=task["session_id"], require_session_match=True
                )
            )
        except FileServiceError as error:
            raise api_error(error.status_code, error.code.lower(), error.code, str(error)) from error
    idempotency_key = payload.idempotency_key or request.headers.get("idempotency-key")
    content_hash = hashlib.sha256(
        json.dumps({"content": payload.content, "file_ids": payload.file_ids}, sort_keys=True).encode()
    ).hexdigest()
    try:
        ledger, created = database.create_request(
            project_id=owner,
            agent_id=agent_id,
            task_id=task_id,
            session_id=task["session_id"],
            content_hash=content_hash,
            idempotency_key=idempotency_key,
        )
    except StorageLimitError as error:
        raise api_error(409, "idempotency_conflict", error.code, str(error)) from error
    if not created:
        if ledger["status"] == "completed" and ledger.get("result"):
            return ledger["result"]
        return JSONResponse(status_code=202, content={"request_id": ledger["id"], "task_id": task_id, "status": ledger["status"]})
    database.add_message(task["session_id"], "user", payload.content)
    runner = _run_task_request(
        request_id=ledger["id"], owner=owner, task=task, content=payload.content,
        file_records=file_records, base_url=str(request.base_url).rstrip("/"),
    )
    if "respond-async" in request.headers.get("prefer", "").lower():
        _track_background(runner)
        return JSONResponse(status_code=202, content={"request_id": ledger["id"], "task_id": task_id, "status": "queued"})
    return await runner


@task_router.get("/v1/requests/{request_id}", summary="查询请求状态")
async def get_request(request_id: str, request: Request):
    item = _db().get_request(_project(request), request_id)
    if not item:
        raise api_error(404, "request_not_found", "REQUEST_NOT_FOUND", "Request not found")
    authorize_capability(
        request, _db(), "task.read", agent_id=item.get("agent_id"),
        session_id=item["session_id"], task_id=item.get("task_id"),
    )
    return item


@task_router.post("/v1/tasks/{task_id}/cancel", summary="取消尚未进入浏览器的请求")
async def cancel_task(task_id: str, request: Request, request_id: str):
    authorize_capability(request, _db(), "task.cancel", task_id=task_id)
    item = _db().get_request(_project(request), request_id)
    if not item or item.get("task_id") != task_id:
        raise api_error(404, "request_not_found", "REQUEST_NOT_FOUND", "Request not found")
    if item["status"] not in {"queued", "waiting_gap"}:
        raise api_error(409, "cannot_cancel_inflight", "CANNOT_CANCEL_INFLIGHT", "Request already entered browser execution")
    if not _scheduler().cancel(request_id):
        raise api_error(409, "cannot_cancel_inflight", "CANNOT_CANCEL_INFLIGHT", "Request already entered browser execution")
    _db().update_request(request_id, "cancelled")
    return {"request_id": request_id, "status": "cancelled"}


@task_router.get("/v1/runtime/status", summary="读取安全的运行状态")
async def runtime_status(request: Request):
    authorize_capability(request, _db(), "runtime.read")
    return _scheduler().snapshot()
