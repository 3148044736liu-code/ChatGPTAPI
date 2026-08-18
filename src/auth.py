"""Resolve legacy and database-backed project bearer tokens."""

from __future__ import annotations

from typing import Any

from src.config import Config

_database: Any = None


def set_auth_database(database: Any) -> None:
    global _database
    _database = database


def resolve_token(token: str) -> dict[str, Any] | None:
    """Return the authenticated project identity, or None for an invalid token."""
    if Config.API_TOKEN or Config.API_USER_TOKENS:
        legacy_owner = Config.owner_for_token(token)
        if legacy_owner is not None:
            return {
                "project_id": legacy_owner,
                "name": legacy_owner,
                "multi_agent": False,
                "source": "legacy_env",
            }
    if _database is None:
        return None
    project = _database.resolve_project_token(token)
    if project is None:
        return None
    project["source"] = "project_registry"
    return project
