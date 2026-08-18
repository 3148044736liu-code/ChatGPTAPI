"""HTTP-level tests for the managed session/file API (no browser needed).

Covers: Bearer auth, owner mapping, session lifecycle, multi-user isolation,
file upload/download (Bearer + signed URL), size limits, per-session file
limit and per-owner disk quota.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

from tests.conftest import ALICE_TOKEN, BOB_TOKEN, LEGACY_TOKEN, auth


# ── Auth ────────────────────────────────────────────────────────


def test_missing_token_returns_401(managed_client):
    resp = managed_client.get("/v1/me")
    assert resp.status_code == 401


def test_invalid_token_returns_401(managed_client):
    resp = managed_client.get("/v1/me", headers=auth("wrong-token"))
    assert resp.status_code == 401


def test_token_owner_mapping(managed_client):
    for token, owner in [
        (LEGACY_TOKEN, "default"),
        (ALICE_TOKEN, "alice"),
        (BOB_TOKEN, "bob"),
    ]:
        resp = managed_client.get("/v1/me", headers=auth(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner_id"] == owner
        assert body["max_concurrent_sessions"] >= 1
        assert body["file_usage"]["used_bytes"] == 0


# ── Sessions ────────────────────────────────────────────────────


def test_session_lifecycle(managed_client):
    created = managed_client.post(
        "/v1/sessions", json={"title": "测试"}, headers=auth(ALICE_TOKEN)
    )
    assert created.status_code == 201
    session = created.json()
    assert session["status"] == "new"
    sid = session["id"]

    listed = managed_client.get("/v1/sessions", headers=auth(ALICE_TOKEN)).json()
    assert any(item["id"] == sid for item in listed["data"])

    fetched = managed_client.get(f"/v1/sessions/{sid}", headers=auth(ALICE_TOKEN))
    assert fetched.status_code == 200

    deleted = managed_client.delete(f"/v1/sessions/{sid}", headers=auth(ALICE_TOKEN))
    assert deleted.status_code == 200
    assert managed_client.get(f"/v1/sessions/{sid}", headers=auth(ALICE_TOKEN)).status_code == 404


def test_session_isolation_between_users(managed_client):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "alice 私有"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]

    # bob cannot see, read or delete alice's session
    assert managed_client.get("/v1/sessions", headers=auth(BOB_TOKEN)).json()["data"] == []
    assert managed_client.get(f"/v1/sessions/{sid}", headers=auth(BOB_TOKEN)).status_code == 404
    assert managed_client.delete(f"/v1/sessions/{sid}", headers=auth(BOB_TOKEN)).status_code == 404


def test_send_message_roundtrip(managed_client, stub_pool):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "t"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]

    resp = managed_client.post(
        f"/v1/sessions/{sid}/messages",
        json={"content": "你好"},
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "echo: 你好"
    assert body["provider_thread_id"] == stub_pool.thread_id

    # pool received the message
    assert len(stub_pool.calls) == 1
    assert stub_pool.calls[0]["message"] == "你好"

    # session activated with the stub thread id
    session = managed_client.get(f"/v1/sessions/{sid}", headers=auth(ALICE_TOKEN)).json()
    assert session["provider_thread_id"] == stub_pool.thread_id
    assert session["status"] == "active"

    # both messages persisted
    messages = managed_client.get(
        f"/v1/sessions/{sid}/messages", headers=auth(ALICE_TOKEN)
    ).json()["data"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "你好"),
        ("assistant", "echo: 你好"),
    ]

    # second message reuses the provider thread
    managed_client.post(
        f"/v1/sessions/{sid}/messages", json={"content": "再来"}, headers=auth(ALICE_TOKEN)
    )
    assert stub_pool.calls[1]["provider_thread_id"] == stub_pool.thread_id


def test_send_message_unknown_file_404(managed_client):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "t"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]
    resp = managed_client.post(
        f"/v1/sessions/{sid}/messages",
        json={"content": "hi", "file_ids": ["file_doesnotexist"]},
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 404


def test_send_message_requires_own_session(managed_client):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "t"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]
    resp = managed_client.post(
        f"/v1/sessions/{sid}/messages", json={"content": "hi"}, headers=auth(BOB_TOKEN)
    )
    assert resp.status_code == 404


# ── Files: upload / download ────────────────────────────────────


def test_upload_and_download_roundtrip(managed_client):
    content = "hello managed files".encode()
    resp = managed_client.post(
        "/v1/files",
        files={"file": ("notes.txt", content, "text/plain")},
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["filename"] == "notes.txt"
    assert body["bytes"] == len(content)
    assert body["source"] == "upload"
    file_id = body["id"]

    # Bearer-token download
    got = managed_client.get(
        f"/v1/files/{file_id}/content", headers=auth(ALICE_TOKEN)
    )
    assert got.status_code == 200
    assert got.content == content

    # Signed-URL download without any Authorization header
    signed = managed_client.get(
        f"/v1/files/{file_id}", headers=auth(ALICE_TOKEN)
    ).json()["download_url"]
    path = urlparse(signed).path
    query = parse_qs(urlparse(signed).query)
    got2 = managed_client.get(
        path, params={k: v[0] for k, v in query.items()}
    )
    assert got2.status_code == 200
    assert got2.content == content


def test_signed_url_rejects_tampering(managed_client):
    file_id = managed_client.post(
        "/v1/files",
        files={"file": ("a.txt", b"data", "text/plain")},
        headers=auth(ALICE_TOKEN),
    ).json()["id"]

    # Expired timestamp
    resp = managed_client.get(
        f"/v1/files/{file_id}/content",
        params={"expires": int(time.time()) - 100, "signature": "x" * 64},
    )
    assert resp.status_code == 401

    # Wrong signature
    resp = managed_client.get(
        f"/v1/files/{file_id}/content",
        params={"expires": int(time.time()) + 600, "signature": "x" * 64},
    )
    assert resp.status_code == 401

    # No auth at all
    assert managed_client.get(f"/v1/files/{file_id}/content").status_code == 401


def test_file_isolation_between_users(managed_client):
    file_id = managed_client.post(
        "/v1/files",
        files={"file": ("secret.txt", b"top secret", "text/plain")},
        headers=auth(ALICE_TOKEN),
    ).json()["id"]

    assert managed_client.get("/v1/files", headers=auth(BOB_TOKEN)).json()["data"] == []
    assert managed_client.get(f"/v1/files/{file_id}", headers=auth(BOB_TOKEN)).status_code == 404
    assert managed_client.delete(f"/v1/files/{file_id}", headers=auth(BOB_TOKEN)).status_code == 404
    # bob's bearer token must not download alice's file
    assert managed_client.get(
        f"/v1/files/{file_id}/content", headers=auth(BOB_TOKEN)
    ).status_code == 401


def test_delete_file_removes_record(managed_client):
    file_id = managed_client.post(
        "/v1/files",
        files={"file": ("gone.txt", b"bye", "text/plain")},
        headers=auth(ALICE_TOKEN),
    ).json()["id"]
    assert managed_client.delete(f"/v1/files/{file_id}", headers=auth(ALICE_TOKEN)).status_code == 200
    assert managed_client.get(f"/v1/files/{file_id}", headers=auth(ALICE_TOKEN)).status_code == 404


# ── Files: limits & quota ───────────────────────────────────────


def test_upload_exceeding_size_limit_returns_413(managed_client):
    big = b"x" * (3 * 1024 * 1024)  # MAX_FILE_SIZE_MB is patched to 2
    resp = managed_client.post(
        "/v1/files",
        files={"file": ("big.bin", big, "application/octet-stream")},
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 413


def test_upload_exceeding_quota_returns_507(managed_client):
    half = b"x" * (600 * 1024)  # USER_FILE_QUOTA_MB is patched to 1 MB
    first = managed_client.post(
        "/v1/files",
        files={"file": ("one.bin", half, "application/octet-stream")},
        headers=auth(ALICE_TOKEN),
    )
    assert first.status_code == 201
    second = managed_client.post(
        "/v1/files",
        files={"file": ("two.bin", half, "application/octet-stream")},
        headers=auth(ALICE_TOKEN),
    )
    assert second.status_code == 507

    # quota is per-owner: bob still has room
    assert managed_client.post(
        "/v1/files",
        files={"file": ("bob.bin", half, "application/octet-stream")},
        headers=auth(BOB_TOKEN),
    ).status_code == 201

    # usage endpoint reflects consumption
    me = managed_client.get("/v1/me", headers=auth(ALICE_TOKEN)).json()
    assert me["file_usage"]["used_bytes"] == len(half)
    assert me["file_usage"]["file_count"] == 1


def test_per_session_file_limit_returns_409(managed_client):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "t"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]

    for name in ("f1.txt", "f2.txt"):
        resp = managed_client.post(
            "/v1/files",
            files={"file": (name, b"x", "text/plain")},
            data={"session_id": sid},
            headers=auth(ALICE_TOKEN),
        )
        assert resp.status_code == 201

    # MAX_FILES_PER_SESSION is patched to 2 — the third must fail
    resp = managed_client.post(
        "/v1/files",
        files={"file": ("f3.txt", b"x", "text/plain")},
        data={"session_id": sid},
        headers=auth(ALICE_TOKEN),
    )
    assert resp.status_code == 409

    # another session is unaffected
    sid2 = managed_client.post(
        "/v1/sessions", json={"title": "t2"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]
    assert managed_client.post(
        "/v1/files",
        files={"file": ("f4.txt", b"x", "text/plain")},
        data={"session_id": sid2},
        headers=auth(ALICE_TOKEN),
    ).status_code == 201


def test_upload_to_foreign_session_404(managed_client):
    sid = managed_client.post(
        "/v1/sessions", json={"title": "t"}, headers=auth(ALICE_TOKEN)
    ).json()["id"]
    resp = managed_client.post(
        "/v1/files",
        files={"file": ("x.txt", b"x", "text/plain")},
        data={"session_id": sid},
        headers=auth(BOB_TOKEN),
    )
    assert resp.status_code == 404


# ── Pool status ─────────────────────────────────────────────────


def test_pool_status(managed_client, stub_pool):
    resp = managed_client.get("/v1/pool/status", headers=auth(ALICE_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"capacity": stub_pool.size, "available": stub_pool.size, "busy": 0}
