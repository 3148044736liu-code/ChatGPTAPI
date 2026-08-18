"""Storage-layer tests: sessions, messages, files, quota helpers, cleanup."""

from __future__ import annotations

from pathlib import Path

from src.config import Config
from src.files.cleanup import sweep_expired_files


# ── Sessions ────────────────────────────────────────────────────


def test_create_and_get_session(database):
    session = database.create_session(owner_id="alice", title="测试会话", provider="chatgpt")
    assert session["id"].startswith("ses_")
    assert session["owner_id"] == "alice"
    assert session["status"] == "new"
    assert session["provider_thread_id"] is None

    fetched = database.get_session(session["id"], "alice")
    assert fetched is not None
    assert fetched["title"] == "测试会话"


def test_session_owner_isolation(database):
    session = database.create_session(owner_id="alice", title="alice", provider="chatgpt")
    # bob must not see alice's session
    assert database.get_session(session["id"], "bob") is None
    assert database.list_sessions("bob") == []
    assert len(database.list_sessions("alice")) == 1
    assert database.delete_session(session["id"], "bob") is False
    assert database.delete_session(session["id"], "alice") is True


def test_activate_session_writes_thread(database):
    session = database.create_session(owner_id="alice", title="t", provider="chatgpt")
    database.activate_session(
        session["id"], "alice", "thread-abc", "https://chatgpt.com/c/thread-abc"
    )
    updated = database.get_session(session["id"], "alice")
    assert updated["provider_thread_id"] == "thread-abc"
    assert updated["provider_thread_url"] == "https://chatgpt.com/c/thread-abc"
    assert updated["status"] == "active"
    assert updated["last_active_at"] is not None


def test_delete_session_cascades_messages(database):
    session = database.create_session(owner_id="alice", title="t", provider="chatgpt")
    database.add_message(session["id"], "user", "hello")
    database.delete_session(session["id"], "alice")
    assert database.list_messages(session["id"], "alice") == []


# ── Messages ────────────────────────────────────────────────────


def test_messages_roundtrip_and_order(database):
    session = database.create_session(owner_id="alice", title="t", provider="chatgpt")
    sid = session["id"]
    database.add_message(sid, "user", "第一句")
    database.add_message(sid, "assistant", "第二句")
    rows = database.list_messages(sid, "alice")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert [r["content"] for r in rows] == ["第一句", "第二句"]
    assert all(r["id"].startswith("msg_") for r in rows)


# ── Files ───────────────────────────────────────────────────────


def _make_file(database, owner="alice", session_id=None, size=100, name="a.txt"):
    return database.create_file(
        owner_id=owner,
        session_id=session_id,
        source="upload",
        original_name=name,
        stored_path=str(Config.FILES_DIR / name),
        mime_type="text/plain",
        size_bytes=size,
        sha256="0" * 64,
    )


def test_file_crud_and_isolation(database):
    record = _make_file(database)
    assert record["id"].startswith("file_")
    assert database.get_file(record["id"], "alice") is not None
    assert database.get_file(record["id"], "bob") is None  # owner filter
    assert database.get_file(record["id"]) is not None  # no owner filter (download path)

    assert len(database.list_files("alice")) == 1
    assert database.list_files("bob") == []

    deleted = database.delete_file(record["id"], "bob")
    assert deleted is None
    deleted = database.delete_file(record["id"], "alice")
    assert deleted is not None
    assert database.get_file(record["id"], "alice") is None


def test_owner_usage_counts_only_live_files(database, monkeypatch):
    _make_file(database, size=100, name="live.txt")
    # Force the second file to be already expired
    monkeypatch.setattr(Config, "FILE_TTL_HOURS", -1)
    _make_file(database, size=900, name="expired.txt")

    usage = database.owner_usage("alice")
    assert usage["total_bytes"] == 100
    assert usage["file_count"] == 1


def test_session_file_count(database, monkeypatch):
    session = database.create_session(owner_id="alice", title="t", provider="chatgpt")
    sid = session["id"]
    _make_file(database, session_id=sid, name="1.txt")
    _make_file(database, session_id=sid, name="2.txt")
    _make_file(database, session_id=None, name="3.txt")
    monkeypatch.setattr(Config, "FILE_TTL_HOURS", -1)
    _make_file(database, session_id=sid, name="4-expired.txt")

    assert database.session_file_count(sid, "alice") == 2


def test_expired_files_and_sweep(database, monkeypatch, tmp_path):
    live_path = Config.FILES_DIR / "live.txt"
    gone_path = Config.FILES_DIR / "gone.txt"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_bytes(b"x")
    gone_path.write_bytes(b"y")

    live = database.create_file(
        owner_id="alice", session_id=None, source="upload",
        original_name="live.txt", stored_path=str(live_path),
        mime_type="text/plain", size_bytes=1, sha256="0" * 64,
    )
    monkeypatch.setattr(Config, "FILE_TTL_HOURS", -1)
    expired = database.create_file(
        owner_id="alice", session_id=None, source="upload",
        original_name="gone.txt", stored_path=str(gone_path),
        mime_type="text/plain", size_bytes=1, sha256="0" * 64,
    )

    expired_ids = {row["id"] for row in database.expired_files()}
    assert expired_ids == {expired["id"]}

    removed = sweep_expired_files(database)
    assert removed == 1
    assert database.get_file(expired["id"], "alice") is None
    assert database.get_file(live["id"], "alice") is not None
    assert not gone_path.exists()  # bytes removed from disk
    assert live_path.exists()


def test_session_file_fk_set_null_on_delete(database):
    session = database.create_session(owner_id="alice", title="t", provider="chatgpt")
    record = _make_file(database, session_id=session["id"])
    database.delete_session(session["id"], "alice")
    # file survives, session_id becomes NULL
    still = database.get_file(record["id"], "alice")
    assert still is not None
    assert still["session_id"] is None
