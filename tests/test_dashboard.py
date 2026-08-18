"""Operations dashboard route and payload tests."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dashboard import dashboard_router, set_dashboard_services
from src.config import Config


def _client(database) -> TestClient:
    pool = SimpleNamespace(
        healthy=1,
        snapshot=lambda: {
            "capacity": 1,
            "available": 1,
            "healthy": 1,
            "busy": 0,
            "queue_depth": 0,
            "single_page": True,
            "current_task_id": None,
            "current_request_id": None,
            "risk": {"state": "healthy", "reason": ""},
        },
    )
    set_dashboard_services(None, pool, database)
    app = FastAPI()
    app.include_router(dashboard_router)
    return TestClient(app, client=("127.0.0.1", 50000))


def test_dashboard_page_contains_operational_views(database):
    with _client(database) as client:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "GPT-FastAPI 运维控制台" in response.text
    assert 'id="view-activity"' in response.text
    assert 'id="view-projects"' in response.text
    assert 'id="view-logs"' in response.text


def test_dashboard_state_returns_task_request_and_conversation(database):
    database.create_project(project_id="project-a", name="Project A")
    database.create_agent("project-a", "writer", "Writer")
    task = database.create_task("project-a", "writer", "Draft", "chatgpt")
    request, _ = database.create_request(
        project_id="project-a",
        agent_id="writer",
        task_id=task["task_id"],
        session_id=task["session_id"],
        content_hash="hash",
        idempotency_key=None,
    )
    database.update_request(
        request["id"],
        "completed",
        provider_thread_id="thread-1",
        provider_thread_url="https://chatgpt.com/c/thread-1",
    )

    with _client(database) as client:
        unauthorized = client.get("/api/dashboard/state")
        response = client.get(
            "/api/dashboard/state",
            headers={"X-Admin-Token": Config.ADMIN_TOKEN},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    activity = response.json()["activity"]
    assert activity["tasks"][0]["task_id"] == task["task_id"]
    assert activity["requests"][0]["id"] == request["id"]
    assert activity["requests"][0]["provider_thread_url"].endswith("/thread-1")
