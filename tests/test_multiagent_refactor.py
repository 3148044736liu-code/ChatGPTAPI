"""Lightweight verification for Agent/Task models and the single-page scheduler."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.chatgpt.detector import normalize_assistant_text
from src.chatgpt.models import ChatResponse
from src.config import Config
from src.provider_errors import AttachmentUploadError, ProviderTimeoutError
from src.api.conversation_router import ConversationRouteError, ConversationRouter


def test_response_normalization_removes_chatgpt_feedback_chrome():
    raw = "E2E_FIXED_OK_20260818\n\nDo you like this personality?"
    assert normalize_assistant_text(raw) == "E2E_FIXED_OK_20260818"


def test_agent_task_and_idempotent_request_models(database):
    database.create_agent("project-a", "writer", "Writer")
    database.set_agent_capabilities("project-a", "writer", {"task.create", "task.send"})
    task = database.create_task("project-a", "writer", "Draft", "chatgpt")
    assert task["task_id"].startswith("tsk_")
    assert task["provider_title"] == task["task_id"]
    assert database.get_session(task["session_id"], "project-a")["task_id"] == task["task_id"]

    first, created = database.create_request(
        project_id="project-a", agent_id="writer", task_id=task["task_id"],
        session_id=task["session_id"], content_hash="abc", idempotency_key="same-key",
    )
    second, created_again = database.create_request(
        project_id="project-a", agent_id="writer", task_id=task["task_id"],
        session_id=task["session_id"], content_hash="abc", idempotency_key="same-key",
    )
    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]


def test_dashboard_activity_exposes_metadata_without_payloads(database):
    database.create_project(project_id="project-a", name="Project A")
    database.create_agent("project-a", "writer", "Writer")
    task = database.create_task("project-a", "writer", "Draft", "chatgpt")
    request, _ = database.create_request(
        project_id="project-a",
        agent_id="writer",
        task_id=task["task_id"],
        session_id=task["session_id"],
        content_hash="secret-content-hash",
        idempotency_key="secret-idempotency-key",
    )
    database.update_request(
        request["id"],
        "completed",
        provider_thread_id="thread-1",
        provider_thread_url="https://chatgpt.com/c/thread-1",
        result={"message": "secret-response"},
    )

    activity = database.dashboard_activity()

    assert activity["tasks"][0]["project_name"] == "Project A"
    assert activity["requests"][0]["task_name"] == "Draft"
    assert activity["requests"][0]["provider_thread_url"].endswith("/thread-1")
    assert activity["request_statuses"] == {"completed": 1}
    assert "result" not in activity["requests"][0]
    assert "content_hash" not in activity["requests"][0]
    assert "idempotency_key" not in activity["requests"][0]


class _RoutingClient:
    def __init__(self):
        self.current = "wrong"
        self.switch_calls = []

    def _extract_thread_id(self):
        return self.current

    async def switch_conversation(self, conversation_id, *, title, conversation_url):
        self.switch_calls.append((conversation_id, title, conversation_url))
        self.current = conversation_id

    async def verify_current_conversation(self, expected):
        return self.current == expected


@pytest.mark.asyncio
async def test_router_passes_persisted_url_and_verifies_switch():
    client = _RoutingClient()
    router = ConversationRouter()
    await router.prepare(
        client,
        task_id="tsk_ABC",
        provider_thread_id="thread-1",
        provider_thread_url="https://chatgpt.com/c/thread-1",
    )
    assert client.switch_calls == [
        ("thread-1", "tsk_ABC", "https://chatgpt.com/c/thread-1")
    ]


@pytest.mark.asyncio
async def test_router_blocks_send_when_conversation_verification_fails():
    client = _RoutingClient()
    with pytest.raises(ConversationRouteError):
        await ConversationRouter().verify(client, "thread-1")


@pytest.mark.asyncio
async def test_chatgpt_upload_verifies_every_attachment(tmp_path, monkeypatch):
    from src.chatgpt.client import ChatGPTClient

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    client = ChatGPTClient.__new__(ChatGPTClient)
    file_input = SimpleNamespace(set_input_files=AsyncMock())
    client._find_file_input = AsyncMock(return_value=file_input)
    client._click_attach_button = AsyncMock(return_value=True)
    client._wait_for_attachment = AsyncMock(side_effect=[True, False])
    with pytest.raises(AttachmentUploadError) as captured:
        await client._upload_files([str(first), str(second)])
    assert captured.value.failed_files == ["second.pdf"]
    assert client._wait_for_attachment.await_count == 2


@pytest.mark.asyncio
async def test_timeout_is_an_error_not_partial_success(monkeypatch):
    import src.chatgpt.client as module
    from src.chatgpt.client import ChatGPTClient

    client = ChatGPTClient.__new__(ChatGPTClient)
    client._page = SimpleNamespace(keyboard=SimpleNamespace(press=AsyncMock()))
    client._detect_page_error = AsyncMock(return_value=None)
    client._dismiss_overlays = AsyncMock()
    client._find_selector = AsyncMock(return_value="#prompt-textarea")
    client._composer_has_text = AsyncMock(return_value=True)
    client._wait_for_send_ready = AsyncMock(return_value=True)
    client._click_send = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "random_delay", AsyncMock())
    monkeypatch.setattr(module, "human_type", AsyncMock())
    monkeypatch.setattr(module, "count_assistant_messages", AsyncMock(return_value=0))
    monkeypatch.setattr(module, "get_latest_assistant_turn_signature", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "wait_for_response_complete", AsyncMock(return_value=False))
    with pytest.raises(ProviderTimeoutError):
        await client.send_message("hello")


class _FakePage:
    def __init__(self):
        self.listeners = {}

    def on(self, event, handler):
        self.listeners[event] = handler

    def is_closed(self):
        return False


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page


class _FakeClient:
    def __init__(self, page):
        self.page = page
        self.current = ""
        self.starts = []
        self.counter = 0

    def _extract_thread_id(self):
        return self.current

    async def new_chat(self):
        self.current = ""

    async def send_message(self, message, file_paths=None):
        self.starts.append(time.monotonic())
        self.counter += 1
        self.current = f"thread-{self.counter}"
        await asyncio.sleep(0.005)
        return ChatResponse(message=message, thread_id=self.current, response_time_ms=5)

    async def rename_current_conversation(self, title):
        return True

    async def open_thread_by_title(self, title, expected):
        self.current = expected
        return True


@pytest.mark.asyncio
async def test_scheduler_uses_one_page_and_start_to_start_gap(tmp_path, monkeypatch):
    import src.api.session_service as module
    from src.api.session_service import SessionWorkerPool

    monkeypatch.setattr(Config, "BROWSER_TASK_GAP_MIN_SECONDS", 0.04)
    monkeypatch.setattr(Config, "BROWSER_TASK_GAP_MAX_SECONDS", 0.04)
    page = _FakePage()
    browser = SimpleNamespace(page=page, _page=page, context=_FakeContext(page))
    client = _FakeClient(page)
    monkeypatch.setattr(SessionWorkerPool, "_client_for", staticmethod(lambda _: client))
    monkeypatch.setattr(module, "capture_generated_files", AsyncMock(return_value=[]))
    pool = SessionWorkerPool()
    await pool.start(browser)
    try:
        first = asyncio.create_task(pool.send(
            session_id="s1", provider_thread_id=None, message="a", file_paths=[],
            generated_dir=tmp_path / "a", task_id="tsk_A", request_id="req_A",
        ))
        second = asyncio.create_task(pool.send(
            session_id="s2", provider_thread_id=None, message="b", file_paths=[],
            generated_dir=tmp_path / "b", task_id="tsk_B", request_id="req_B",
        ))
        await asyncio.gather(first, second)
    finally:
        await pool.close()
    assert len(browser.context.pages) == 1
    assert browser.context.new_page_calls == 0
    # Windows timers may resume a few milliseconds early at this tiny test
    # scale; production gaps are measured in tens of seconds.
    assert client.starts[1] - client.starts[0] >= 0.025
    assert 0.04 <= pool.last_sampled_gap_seconds <= 0.04
