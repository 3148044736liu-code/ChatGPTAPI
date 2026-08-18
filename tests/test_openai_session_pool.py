"""Tests for the optional /v1/chat/completions → session pool routing."""

from __future__ import annotations

from tests.conftest import ALICE_TOKEN, BOB_TOKEN, auth


def _chat_body(text="你好", session_id=None):
    body = {
        "model": "catgpt-browser",
        "messages": [{"role": "user", "content": text}],
    }
    if session_id:
        body["session_id"] = session_id
    return body


def test_legacy_path_unchanged_without_session_id(openai_client):
    """Without session_id and with the switch off, requests must NOT be
    routed through the pool (the legacy client path is used instead, which
    is not initialized in tests and therefore returns 503)."""
    resp = openai_client.post(
        "/v1/chat/completions", json=_chat_body(), headers=auth(ALICE_TOKEN)
    )
    assert resp.status_code == 503


def test_explicit_session_id_routes_through_pool(openai_client, database, stub_pool):
    session = database.create_session(owner_id="alice", title="s", provider="chatgpt")
    sid = session["id"]

    resp = openai_client.post(
        "/v1/chat/completions",
        json=_chat_body("帮我总结一下", session_id=sid),
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["session_id"] == sid
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "echo: 帮我总结一下"
    assert body["choices"][0]["finish_reason"] == "stop"

    # Pool was used and messages were persisted
    assert len(stub_pool.calls) == 1
    messages = database.list_messages(sid, "alice")
    assert [m["role"] for m in messages] == ["user", "assistant"]

    # Session activated with stub thread id
    assert database.get_session(sid, "alice")["provider_thread_id"] == stub_pool.thread_id


def test_foreign_session_id_returns_404(openai_client, database):
    session = database.create_session(owner_id="alice", title="s", provider="chatgpt")
    resp = openai_client.post(
        "/v1/chat/completions",
        json=_chat_body(session_id=session["id"]),
        headers=auth(BOB_TOKEN),
    )
    assert resp.status_code == 404


def test_unknown_session_id_returns_404(openai_client):
    resp = openai_client.post(
        "/v1/chat/completions",
        json=_chat_body(session_id="ses_missing"),
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 404


def test_ephemeral_session_when_switch_enabled(openai_client, monkeypatch, database):
    from src.config import Config

    monkeypatch.setattr(Config, "OPENAI_USE_SESSION_POOL", True)
    resp = openai_client.post(
        "/v1/chat/completions", json=_chat_body("临时会话"), headers=auth(ALICE_TOKEN)
    )
    assert resp.status_code == 200
    body = resp.json()
    sid = body["session_id"]
    assert sid and sid.startswith("ses_")

    # The ephemeral session exists and belongs to alice
    session = database.get_session(sid, "alice")
    assert session is not None
    assert session["provider_thread_id"] is not None


def test_stream_with_session_rejected(openai_client, database):
    session = database.create_session(owner_id="alice", title="s", provider="chatgpt")
    body = _chat_body(session_id=session["id"])
    body["stream"] = True
    resp = openai_client.post(
        "/v1/chat/completions", json=body, headers=auth(ALICE_TOKEN)
    )
    assert resp.status_code == 400


def test_session_pool_messages_isolated_per_owner(openai_client, database, stub_pool):
    sa = database.create_session(owner_id="alice", title="a", provider="chatgpt")["id"]
    sb = database.create_session(owner_id="bob", title="b", provider="chatgpt")["id"]

    openai_client.post(
        "/v1/chat/completions",
        json=_chat_body("alice 的消息", session_id=sa),
        headers=auth(ALICE_TOKEN),
    )
    openai_client.post(
        "/v1/chat/completions",
        json=_chat_body("bob 的消息", session_id=sb),
        headers=auth(BOB_TOKEN),
    )

    assert database.list_messages(sa, "bob") == []
    assert database.list_messages(sb, "alice") == []
    a_msgs = database.list_messages(sa, "alice")
    assert any("alice 的消息" in m["content"] for m in a_msgs)
