"""Database-backed sessions and managed file APIs."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from src.api.session_service import SessionWorkerPool, safe_owner_segment
from src.config import Config
from src.auth import resolve_token
from src.files.service import FileService, FileServiceError
from src.capabilities import authorize_capability
from src.api.errors import api_error, provider_error
from src.provider_errors import ProviderError
from src.log import setup_logging
from src.storage.database import Database

log = setup_logging("managed_routes")

managed_router = APIRouter(tags=["多用户会话与文件"])

# Bearer Token 安全方案：让 /docs 页面出现 Authorize 按钮。
# 实际鉴权由 BearerTokenMiddleware 统一完成，这里只负责在 OpenAPI
# 文档中声明安全要求（auto_error=False 避免与中间件重复报错）。
bearer_security = HTTPBearer(
    auto_error=False,
    description="运维仪表盘生成的项目 Token，或 .env 中保留的兼容访问令牌",
)

_database: Database | None = None
_pool: SessionWorkerPool | None = None
_file_service: FileService | None = None


def set_managed_services(
    database: Database | None,
    pool: SessionWorkerPool | None,
) -> None:
    global _database, _pool, _file_service
    _database = database
    _pool = pool
    _file_service = FileService(database) if database is not None else None


def _db() -> Database:
    if _database is None:
        raise HTTPException(status_code=503, detail="Database service not initialized")
    return _database


def _workers() -> SessionWorkerPool:
    if _pool is None:
        raise HTTPException(status_code=503, detail="Session worker pool not initialized")
    return _pool


def _files() -> FileService:
    if _file_service is None:
        raise HTTPException(status_code=503, detail="File service not initialized")
    return _file_service


def _file_error(error: FileServiceError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"type": error.code.lower(), "message": str(error)},
    )


def _owner(request: Request) -> str:
    return request.scope.get("catgpt.owner_id", "default")


def _signature(file_id: str, owner_id: str, expires: int) -> str:
    payload = f"{file_id}:{owner_id}:{expires}".encode()
    return hmac.new(
        Config.DOWNLOAD_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _file_payload(record: dict[str, Any], request: Request) -> dict[str, Any]:
    expires = int(time.time()) + 600
    signature = _signature(record["id"], record["owner_id"], expires)
    base = str(request.base_url).rstrip("/")
    return {
        "id": record["id"],
        "object": "file",
        "session_id": record["session_id"],
        "source": record["source"],
        "filename": record["original_name"],
        "bytes": record["size_bytes"],
        "mime_type": record["mime_type"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "download_url": (
            f"{base}/v1/files/{record['id']}/content"
            f"?expires={expires}&signature={signature}"
        ),
    }


def _authorized_download(request: Request, record: dict[str, Any], expires: int | None, signature: str | None) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        identity = resolve_token(auth[7:])
        if identity and identity["project_id"] == record["owner_id"]:
            return True
    if not expires or not signature or expires < int(time.time()):
        return False
    expected = _signature(record["id"], record["owner_id"], expires)
    return hmac.compare_digest(signature, expected)


def _check_owner_quota(database: Database, owner: str, extra_bytes: int = 0) -> dict[str, Any]:
    """Raise 507 when the owner's live files would exceed the disk quota.

    Returns the current usage dict so callers can reuse it.
    """
    usage = database.owner_usage(owner)
    quota_bytes = Config.USER_FILE_QUOTA_MB * 1024 * 1024
    if usage["total_bytes"] + extra_bytes > quota_bytes:
        raise HTTPException(
            status_code=507,
            detail=(
                f"文件存储空间不足：当前用户已使用 "
                f"{usage['total_bytes'] / 1024 / 1024:.1f} MB，"
                f"配额为 {Config.USER_FILE_QUOTA_MB} MB"
            ),
        )
    return usage


class SessionCreate(BaseModel):
    title: str = Field(
        default="New session",
        max_length=200,
        description="会话标题，仅用于展示",
    )
    agent_id: str | None = Field(
        default=None,
        max_length=120,
        description="多智能体项目中的智能体标识；同一智能体应持续复用返回的 session_id",
    )


class SessionMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=100_000, description="发送给 ChatGPT 的消息内容")
    file_ids: list[str] = Field(
        default_factory=list,
        max_items=10,
        description="要作为附件发送的文件 id 列表（必须先通过 /v1/files 上传）",
    )


@managed_router.get(
    "/v1/me",
    summary="查看当前调用者",
    description="返回当前 Bearer Token 对应的用户 id、并发会话上限，以及该用户的文件存储用量与配额。",
    dependencies=[Depends(bearer_security)],
)
async def current_user(request: Request):
    authorize_capability(request, _db(), "identity.read", agent_id=request.headers.get("x-agent-id"))
    owner = _owner(request)
    project = request.scope.get("catgpt.project") or {}
    payload: dict[str, Any] = {
        "owner_id": owner,
        "project_id": owner,
        "project_name": project.get("name", owner),
        "multi_agent": bool(project.get("multi_agent", False)),
        "token_source": project.get("source", "legacy_env"),
        "max_concurrent_sessions": Config.MAX_CONCURRENT_SESSIONS,
    }
    if _database is not None:
        usage = _database.owner_usage(owner)
        payload["file_usage"] = {
            "used_bytes": usage["total_bytes"],
            "file_count": usage["file_count"],
            "quota_bytes": Config.USER_FILE_QUOTA_MB * 1024 * 1024,
        }
    return payload


@managed_router.get(
    "/v1/pool/status",
    summary="查看浏览器并发池状态",
    description="返回会话 worker 池的容量、空闲与占用中的浏览器页面数量。",
    dependencies=[Depends(bearer_security)],
)
async def pool_status():
    pool = _workers()
    return {
        "capacity": pool.size,
        "available": pool.available,
        "busy": pool.size - pool.available,
    }


@managed_router.post(
    "/v1/sessions",
    status_code=201,
    summary="新建会话",
    description=(
        "创建一个内部会话。第一次发送消息后会自动在 ChatGPT 网页侧新建对话，"
        "并把真实 thread id 写入 provider_thread_id。"
    ),
    dependencies=[Depends(bearer_security)],
)
async def create_session(payload: SessionCreate, request: Request):
    project = request.scope.get("catgpt.project") or {}
    if project.get("multi_agent") and not (payload.agent_id or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Multi-agent projects must provide agent_id when creating a session",
        )
    authorize_capability(request, _db(), "session.create", agent_id=(payload.agent_id or "").strip() or None)
    return _db().create_session(
        owner_id=_owner(request),
        title=payload.title.strip() or "New session",
        provider=Config.PROVIDER,
        agent_id=(payload.agent_id or "").strip() or None,
    )


@managed_router.get(
    "/v1/sessions",
    summary="会话列表",
    description="列出当前用户的全部会话，按最近活跃时间倒序。",
    dependencies=[Depends(bearer_security)],
)
async def list_sessions(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    authorize_capability(request, _db(), "session.list", agent_id=request.headers.get("x-agent-id"))
    return {"data": _db().list_sessions(_owner(request), limit)}


@managed_router.get(
    "/v1/sessions/{session_id}",
    summary="查询单个会话",
    description="按会话 id 查询会话详情。只能查询属于自己的会话。",
    dependencies=[Depends(bearer_security)],
)
async def get_session(session_id: str, request: Request):
    authorize_capability(request, _db(), "session.read", session_id=session_id)
    session = _db().get_session(session_id, _owner(request))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@managed_router.delete(
    "/v1/sessions/{session_id}",
    summary="删除会话",
    description="删除内部会话及其消息记录（ChatGPT 网页侧对话不会被删除）。",
    dependencies=[Depends(bearer_security)],
)
async def delete_session(session_id: str, request: Request):
    authorize_capability(request, _db(), "session.delete", session_id=session_id)
    if not _db().delete_session(session_id, _owner(request)):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"id": session_id, "deleted": True}


@managed_router.get(
    "/v1/sessions/{session_id}/messages",
    summary="会话消息记录",
    description="按时间顺序返回该会话内保存的全部消息。",
    dependencies=[Depends(bearer_security)],
)
async def list_messages(session_id: str, request: Request):
    authorize_capability(request, _db(), "session.history.read", session_id=session_id)
    owner = _owner(request)
    if not _db().get_session(session_id, owner):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"data": _db().list_messages(session_id, owner)}


@managed_router.post(
    "/v1/sessions/{session_id}/messages",
    summary="发送消息（单 Worker 串行入口）",
    description=(
        "向指定会话发送一条消息，由单浏览器 Worker 队列执行（同一会话内串行，"
        "所有会话共用 1 个 Worker 串行执行，前后请求至少间隔 30 秒）。"
        "可携带已上传文件作为附件；回复中若检测到"
        "ChatGPT 生成的文件，会写入文件库并在 attachments 中返回下载链接。"
    ),
    dependencies=[Depends(bearer_security)],
)
async def send_session_message(session_id: str, payload: SessionMessageRequest, request: Request):
    database = _db()
    owner = _owner(request)
    session = database.get_session(session_id, owner)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    authorize_capability(
        request, database, "chat.send", session_id=session_id, task_id=session.get("task_id")
    )

    file_paths: list[str] = []
    strict_project_files = (
        (request.scope.get("catgpt.project") or {}).get("source")
        == "project_registry"
    )
    for file_id in payload.file_ids:
        try:
            record = _files().resolve_for_session(
                file_id,
                owner_id=owner,
                session_id=session_id,
                require_session_match=strict_project_files,
            )
        except FileServiceError as error:
            raise _file_error(error) from error
        file_paths.append(record["stored_path"])

    database.set_session_status(session_id, owner, "busy")
    database.add_message(session_id, "user", payload.content)
    generated_dir = (
        Config.FILES_DIR / safe_owner_segment(owner) / session_id / "generated"
    )

    try:
        result, generated_paths = await _workers().send(
            session_id=session_id,
            provider_thread_id=session.get("provider_thread_id"),
            provider_thread_url=session.get("provider_thread_url"),
            message=payload.content,
            file_paths=file_paths,
            generated_dir=generated_dir,
            task_id=session.get("task_id"),
        )
        if result.thread_id:
            database.activate_session(
                session_id, owner, result.thread_id, result.thread_url or None
            )
        else:
            database.set_session_status(session_id, owner, "active")
    except ProviderError as error:
        database.set_session_status(session_id, owner, "error")
        raise provider_error(error, session_id=session_id) from error
    except Exception as error:
        database.set_session_status(session_id, owner, "error")
        raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider request failed", retryable=True, session_id=session_id) from error

    managed_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for image in result.images or []:
        if image.local_path and Path(image.local_path).is_file():
            generated_paths.append(Path(image.local_path))

    for path in generated_paths:
        path = path.resolve()
        if str(path) in seen_paths or not path.is_file():
            continue
        seen_paths.add(str(path))
        try:
            record = _files().register_generated(
                path,
                owner_id=owner,
                session_id=session_id,
                original_name=path.name,
            )
        except FileServiceError as error:
            log.warning("Skipping generated file %s: %s", path.name, error)
            continue
        managed_files.append(_file_payload(record, request))

    message_text = result.message
    if managed_files:
        links = "\n".join(
            f"[下载 {item['filename']}]({item['download_url']})"
            for item in managed_files
        )
        message_text = f"{message_text}\n\n{links}".strip()

    assistant_message = database.add_message(session_id, "assistant", message_text)

    return {
        "id": assistant_message["id"],
        "session_id": session_id,
        "provider_thread_id": result.thread_id or session.get("provider_thread_id"),
        "provider_thread_url": result.thread_url or session.get("provider_thread_url"),
        "message": message_text,
        "response_time_ms": result.response_time_ms,
        "attachments": managed_files,
        "pool": {
            "capacity": _workers().size,
            "available": _workers().available,
        },
    }


@managed_router.post(
    "/v1/files",
    status_code=201,
    summary="上传文件",
    description=(
        "上传一个文件到当前用户的文件库（multipart/form-data，字段名 file）。"
        "可选通过 session_id 绑定到某个会话，之后发消息时用 file_ids 引用。"
        "返回包含带签名的临时下载链接 download_url。"
    ),
    dependencies=[Depends(bearer_security)],
)
async def upload_file(
    request: Request,
    file: UploadFile = File(..., description="要上传的文件"),
    session_id: str | None = Form(default=None, description="可选：绑定到的会话 id"),
):
    owner = _owner(request)
    database = _db()
    authorize_capability(request, database, "file.upload", session_id=session_id)
    if (
        (request.scope.get("catgpt.project") or {}).get("source")
        == "project_registry"
        and not session_id
    ):
        raise HTTPException(
            status_code=422,
            detail="Project token uploads must include session_id",
        )
    try:
        record = await _files().create_from_upload(
            file,
            owner_id=owner,
            session_id=session_id,
        )
    except FileServiceError as error:
        raise _file_error(error) from error
    return _file_payload(record, request)


@managed_router.get(
    "/v1/files",
    summary="文件列表",
    description="列出当前用户的文件记录（含上传与 ChatGPT 生成文件），按创建时间倒序。",
    dependencies=[Depends(bearer_security)],
)
async def list_files(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    authorize_capability(request, _db(), "file.list", agent_id=request.headers.get("x-agent-id"))
    return {
        "data": [
            _file_payload(record, request)
            for record in _db().list_files(_owner(request), limit)
        ]
    }


@managed_router.get(
    "/v1/files/{file_id}",
    summary="查询文件信息",
    description="按文件 id 查询文件元数据与临时下载链接。只能查询属于自己的文件。",
    dependencies=[Depends(bearer_security)],
)
async def get_file_metadata(file_id: str, request: Request):
    authorize_capability(request, _db(), "file.read", agent_id=request.headers.get("x-agent-id"))
    record = _db().get_file(file_id, _owner(request))
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    return _file_payload(record, request)


@managed_router.delete(
    "/v1/files/{file_id}",
    summary="删除文件",
    description="删除文件记录及磁盘上的文件内容。",
    dependencies=[Depends(bearer_security)],
)
async def delete_file(file_id: str, request: Request):
    authorize_capability(request, _db(), "file.delete", agent_id=request.headers.get("x-agent-id"))
    try:
        deleted = _files().delete(file_id, owner_id=_owner(request))
    except FileServiceError as error:
        raise _file_error(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    return {"id": file_id, "deleted": True}


@managed_router.get(
    "/v1/files/{file_id}/content",
    summary="下载文件内容",
    description=(
        "下载文件内容。两种授权方式任选其一："
        "1) 携带本用户的 Bearer Token；"
        "2) 使用文件接口返回的带 expires 与 signature 的签名链接。"
    ),
    dependencies=[Depends(bearer_security)],
)
async def download_file(
    file_id: str,
    request: Request,
    expires: int | None = Query(default=None, description="签名链接的过期时间戳"),
    signature: str | None = Query(default=None, description="下载链接签名"),
):
    record = _db().get_file(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="File content expired")
    if not _authorized_download(request, record, expires, signature):
        raise HTTPException(status_code=401, detail="Invalid or expired download authorization")
    path = Path(record["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="File content expired")
    return FileResponse(
        path,
        media_type=record["mime_type"],
        filename=record["original_name"],
    )
