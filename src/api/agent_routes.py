"""Project-scoped Agent and Capability management APIs."""

from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.errors import api_error
from src.capabilities import CAPABILITIES, authorize_capability, capability_catalog
from src.config import Config
from src.storage.database import Database


agent_router = APIRouter(tags=["Agents 与能力"])
_database: Database | None = None
_id_pattern = re.compile(r"^[A-Za-z0-9_.-]{2,120}$")


def set_agent_services(database: Database | None) -> None:
    global _database
    _database = database


def _db() -> Database:
    if _database is None:
        raise api_error(503, "service_unavailable", "DATABASE_UNAVAILABLE", "Database service is unavailable", retryable=True)
    return _database


def _project(request: Request) -> str:
    return str(request.scope.get("catgpt.owner_id", ""))


def _require_project_admin(request: Request) -> None:
    provided = request.headers.get("x-admin-token", "")
    if not Config.ADMIN_TOKEN:
        raise api_error(503, "admin_not_configured", "ADMIN_NOT_CONFIGURED", "Admin authentication is not configured")
    if not provided or not hmac.compare_digest(provided, Config.ADMIN_TOKEN):
        raise api_error(403, "admin_required", "ADMIN_REQUIRED", "Project administration requires X-Admin-Token")


class AgentCreate(BaseModel):
    agent_id: str = Field(..., min_length=2, max_length=120)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list)


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class CapabilityUpdate(BaseModel):
    capabilities: list[str] = Field(default_factory=list)


def _validate_agent_id(value: str) -> str:
    value = value.strip()
    if not _id_pattern.fullmatch(value):
        raise api_error(422, "validation_error", "INVALID_AGENT_ID", "agent_id contains unsupported characters")
    return value


def _validate_capabilities(items: list[str]) -> set[str]:
    values = {str(item).strip() for item in items if str(item).strip()}
    unknown = sorted(values - CAPABILITIES)
    if unknown:
        raise api_error(422, "validation_error", "UNKNOWN_CAPABILITY", f"Unknown capabilities: {', '.join(unknown)}")
    return values


@agent_router.get("/v1/capabilities", summary="列出 Gateway 支持的能力")
async def list_capabilities():
    return {"data": capability_catalog()}


@agent_router.get("/v1/agents", summary="列出当前项目的 Agents")
async def list_agents(request: Request):
    try:
        _require_project_admin(request)
    except HTTPException:
        authorize_capability(request, _db(), "agent.read")
    return {"data": _db().list_agents(_project(request))}


@agent_router.post("/v1/agents", status_code=201, summary="创建 Agent")
async def create_agent(payload: AgentCreate, request: Request):
    _require_project_admin(request)
    agent_id = _validate_agent_id(payload.agent_id)
    if _db().get_agent(_project(request), agent_id):
        raise api_error(409, "agent_exists", "AGENT_EXISTS", "Agent already exists")
    capabilities = _validate_capabilities(payload.capabilities)
    agent = _db().create_agent(
        _project(request), agent_id, payload.name.strip(), payload.description.strip()
    )
    _db().set_agent_capabilities(_project(request), agent_id, capabilities)
    agent["capabilities"] = sorted(capabilities)
    return agent


@agent_router.get("/v1/agents/{agent_id}", summary="查询 Agent")
async def get_agent(agent_id: str, request: Request):
    trusted = _validate_agent_id(agent_id)
    try:
        _require_project_admin(request)
    except HTTPException:
        caller = authorize_capability(request, _db(), "agent.read")
        if caller != trusted:
            raise api_error(403, "capability_denied", "CAPABILITY_DENIED", "Agents may only inspect themselves")
    agent = _db().get_agent(_project(request), trusted)
    if not agent:
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found")
    agent["capabilities"] = _db().get_agent_capabilities(_project(request), trusted)
    return agent


@agent_router.patch("/v1/agents/{agent_id}", summary="更新 Agent")
async def update_agent(agent_id: str, payload: AgentUpdate, request: Request):
    _require_project_admin(request)
    agent = _db().update_agent(
        _project(request), _validate_agent_id(agent_id),
        name=payload.name.strip() if payload.name is not None else None,
        description=payload.description.strip() if payload.description is not None else None,
        enabled=payload.enabled,
    )
    if not agent:
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found")
    return agent


@agent_router.delete("/v1/agents/{agent_id}", summary="停用 Agent（保留历史会话）")
async def delete_agent(agent_id: str, request: Request):
    _require_project_admin(request)
    trusted = _validate_agent_id(agent_id)
    if not _db().disable_agent(_project(request), trusted):
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found")
    return {"id": trusted, "disabled": True, "history_retained": True}


@agent_router.get("/v1/agents/{agent_id}/capabilities", summary="查询 Agent 能力")
async def get_capabilities(agent_id: str, request: Request):
    trusted = _validate_agent_id(agent_id)
    try:
        _require_project_admin(request)
    except HTTPException:
        caller = authorize_capability(request, _db(), "agent.read")
        if caller != trusted:
            raise api_error(403, "capability_denied", "CAPABILITY_DENIED", "Agents may only inspect themselves")
    if not _db().get_agent(_project(request), trusted):
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found")
    return {"agent_id": trusted, "data": _db().get_agent_capabilities(_project(request), trusted)}


@agent_router.put("/v1/agents/{agent_id}/capabilities", summary="替换 Agent 能力")
async def put_capabilities(agent_id: str, payload: CapabilityUpdate, request: Request):
    _require_project_admin(request)
    trusted = _validate_agent_id(agent_id)
    values = _validate_capabilities(payload.capabilities)
    try:
        saved = _db().set_agent_capabilities(_project(request), trusted, values)
    except KeyError as error:
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found") from error
    return {"agent_id": trusted, "data": saved}
