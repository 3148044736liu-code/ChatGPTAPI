"""Agent capability catalog and authorization guard."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from src.api.errors import api_error
from src.config import Config
from src.storage.database import Database


CAPABILITIES = {
    "identity.read", "agent.read", "agent.manage",
    "session.create", "session.list", "session.read", "session.delete", "session.history.read",
    "chat.send", "chat.openai_compatible", "response.create", "tool.request",
    "file.upload", "file.list", "file.read", "file.download", "file.delete",
    "image.generate", "runtime.read",
    "task.create", "task.read", "task.list", "task.send", "task.cancel",
}

PROVIDER_CAPABILITIES = {
    "chatgpt": {"chat", "files", "images_input", "images_generate", "tools"},
    "claude": {"chat", "files", "images_input", "tools"},
}


def capability_catalog() -> list[dict[str, Any]]:
    provider = PROVIDER_CAPABILITIES.get(Config.PROVIDER, {"chat"})
    requirements = {
        "image.generate": "images_generate",
        "file.upload": "files",
        "file.download": "files",
        "tool.request": "tools",
    }
    return [
        {
            "id": capability,
            "available": requirements.get(capability) is None or requirements[capability] in provider,
        }
        for capability in sorted(CAPABILITIES)
    ]


def authorize_capability(
    request: Request,
    database: Database,
    capability: str,
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> str | None:
    """Return the trusted agent id, enforcing task/session bindings when present."""
    project_id = str(request.scope.get("catgpt.owner_id", ""))
    project = request.scope.get("catgpt.project") or {}
    if project.get("source") == "legacy_env" or not project.get("multi_agent"):
        return agent_id or request.headers.get("x-agent-id")

    trusted = agent_id or request.headers.get("x-agent-id")
    if task_id:
        task = database.get_task(project_id, task_id)
        if not task:
            raise api_error(404, "task_not_found", "TASK_NOT_FOUND", "Task not found")
        if session_id and task["session_id"] != session_id:
            raise api_error(409, "task_session_mismatch", "TASK_SESSION_MISMATCH", "Task and session do not match")
        if trusted and trusted != task["agent_id"]:
            raise api_error(403, "agent_mismatch", "AGENT_MISMATCH", "Agent does not own this task")
        trusted = task["agent_id"]
    if session_id:
        session = database.get_session(session_id, project_id)
        if not session:
            raise api_error(404, "session_not_found", "SESSION_NOT_FOUND", "Session not found")
        if trusted and trusted != session.get("agent_id"):
            raise api_error(403, "agent_mismatch", "AGENT_MISMATCH", "Agent does not own this session")
        trusted = session.get("agent_id")
    if not trusted:
        raise api_error(400, "agent_required", "AGENT_REQUIRED", "X-Agent-ID or a bound task/session is required")
    agent = database.get_agent(project_id, trusted)
    if not agent:
        raise api_error(404, "agent_not_found", "AGENT_NOT_FOUND", "Agent not found")
    if not agent["enabled"]:
        raise api_error(403, "agent_disabled", "AGENT_DISABLED", "Agent is disabled")
    if capability not in database.get_agent_capabilities(project_id, trusted):
        raise api_error(
            403,
            "capability_denied",
            "CAPABILITY_DENIED",
            f"Agent is not authorized for {capability}",
            capability=capability,
            agent_id=trusted,
        )
    return trusted
