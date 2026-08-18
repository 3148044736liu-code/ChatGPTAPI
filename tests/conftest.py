"""Shared test fixtures: isolated config, temp database, in-memory app.

These tests cover the storage layer, auth middleware, session/file APIs and
the OpenAI session-pool routing WITHOUT launching any browser. The browser
worker pool is replaced by a duck-typed stub.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Config

LEGACY_TOKEN = "legacy-token"
ALICE_TOKEN = "alice-token"
BOB_TOKEN = "bob-token"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point all runtime paths/secrets/limits at temp values for isolation."""
    monkeypatch.setattr(Config, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(Config, "TEMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(Config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(Config, "DATABASE_PATH", tmp_path / "data" / "test.db")
    monkeypatch.setattr(Config, "API_TOKEN", LEGACY_TOKEN)
    monkeypatch.setattr(Config, "API_USER_TOKENS", f"alice:{ALICE_TOKEN},bob:{BOB_TOKEN}")
    monkeypatch.setattr(Config, "DOWNLOAD_SECRET", "test-download-secret")
    monkeypatch.setattr(Config, "MAX_FILE_SIZE_MB", 2)
    monkeypatch.setattr(Config, "USER_FILE_QUOTA_MB", 1)
    monkeypatch.setattr(Config, "MAX_FILES_PER_SESSION", 2)
    monkeypatch.setattr(Config, "OPENAI_USE_SESSION_POOL", False)
    monkeypatch.setattr(Config, "ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setattr(Config, "BOOTSTRAP_ADMIN_TOKEN", "")
    monkeypatch.setattr(Config, "DASHBOARD_REQUIRE_ADMIN_TOKEN", True)
    monkeypatch.setattr(Config, "CORS_ALLOWED_ORIGINS", ())
    monkeypatch.setattr(Config, "BROWSER_TASK_GAP_MIN_SECONDS", 0.0)
    monkeypatch.setattr(Config, "BROWSER_TASK_GAP_MAX_SECONDS", 0.0)
    monkeypatch.setattr(Config, "MAX_BROWSER_QUEUE_DEPTH", 20)
    (tmp_path / "files").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture()
def database():
    from src.storage.database import Database

    return Database(Config.DATABASE_PATH)


class StubPool:
    """Duck-typed stand-in for SessionWorkerPool (no browser involved)."""

    def __init__(self) -> None:
        self.size = 3
        self.calls: list[dict] = []
        self.generated: list = []
        self.reply = "echo: {message}"
        self.thread_id = "thread-stub-001"

    @property
    def available(self) -> int:
        return self.size

    async def send(
        self, *, session_id, provider_thread_id, message, file_paths, generated_dir,
        provider_thread_url=None, task_id=None, request_id=None,
    ):
        from src.chatgpt.models import ChatResponse

        self.calls.append(
            {
                "session_id": session_id,
                "provider_thread_id": provider_thread_id,
                "provider_thread_url": provider_thread_url,
                "message": message,
                "file_paths": list(file_paths or []),
                "generated_dir": generated_dir,
                "task_id": task_id,
                "request_id": request_id,
            }
        )
        result = ChatResponse(
            message=self.reply.format(message=message),
            thread_id=self.thread_id,
            response_time_ms=7,
        )
        return result, list(self.generated)


@pytest.fixture()
def stub_pool():
    return StubPool()


@pytest.fixture()
def managed_client(database, stub_pool):
    """TestClient over the managed session/file API with auth middleware."""
    from src.api.managed_routes import managed_router, set_managed_services
    from src.api.server import BearerTokenMiddleware

    set_managed_services(database, stub_pool)
    app = FastAPI()
    app.add_middleware(BearerTokenMiddleware)
    app.include_router(managed_router)
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def openai_client(database, stub_pool):
    """TestClient over the OpenAI-compatible API wired to the stub pool."""
    from src.api.openai_routes import openai_router, set_openai_services
    from src.api.server import BearerTokenMiddleware

    set_openai_services(database, stub_pool)
    app = FastAPI()
    app.add_middleware(BearerTokenMiddleware)
    app.include_router(openai_router)
    with TestClient(app) as client:
        yield client


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
