"""Small SQLite repository for session and file metadata."""

from __future__ import annotations

import sqlite3
import hashlib
import json
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import Config
from src.token_vault import protect_token, unprotect_token


class StorageLimitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_after(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class Database:
    def __init__(self, path: Path | None = None) -> None:
        Config.ensure_dirs()
        self.path = path or Config.DATABASE_PATH
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_thread_id TEXT,
                    provider_thread_url TEXT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_active_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
                    ON sessions(owner_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session_created
                    ON messages(session_id, created_at);

                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT,
                    source TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_files_owner_created
                    ON files(owner_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    multi_agent INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_projects_updated
                    ON projects(updated_at DESC);

                CREATE TABLE IF NOT EXISTS agents (
                    project_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, id)
                );

                CREATE TABLE IF NOT EXISTS agent_capabilities (
                    project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, agent_id, capability),
                    FOREIGN KEY(project_id, agent_id) REFERENCES agents(project_id, id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    provider_thread_id TEXT,
                    provider_thread_url TEXT,
                    provider_title TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_started_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
                    FOREIGN KEY(project_id, agent_id) REFERENCES agents(project_id, id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_project_agent_updated
                    ON tasks(project_id, agent_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    agent_id TEXT,
                    task_id TEXT,
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_thread_id TEXT,
                    provider_thread_url TEXT,
                    result_json TEXT,
                    error_type TEXT,
                    sent_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE RESTRICT,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_requests_project_created
                    ON requests(project_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_idempotency
                    ON requests(project_id, agent_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                """
            )
            session_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "agent_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
            if "task_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN task_id TEXT")
            if "provider_thread_url" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN provider_thread_url TEXT")
            task_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "provider_thread_url" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN provider_thread_url TEXT")
            request_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(requests)").fetchall()
            }
            if "provider_thread_url" not in request_columns:
                connection.execute("ALTER TABLE requests ADD COLUMN provider_thread_url TEXT")
            file_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(files)").fetchall()
            }
            if "status" not in file_columns:
                connection.execute("ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'READY'")
            if "error_message" not in file_columns:
                connection.execute("ALTER TABLE files ADD COLUMN error_message TEXT")
            project_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "token_ciphertext" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN token_ciphertext TEXT")
            if "deleted_at" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_session(
        self,
        owner_id: str,
        title: str,
        provider: str,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = f"ses_{uuid.uuid4().hex}"
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions
                    (id, owner_id, provider, title, status, created_at, updated_at, agent_id, task_id)
                VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (session_id, owner_id, provider, title, timestamp, timestamp, agent_id, task_id),
            )
        return self.get_session(session_id, owner_id)  # type: ignore[return-value]

    def get_session(self, session_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ? AND owner_id = ?",
                (session_id, owner_id),
            ).fetchone()
        return self._row(row)

    def list_sessions(self, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE owner_id = ?
                ORDER BY COALESCE(last_active_at, created_at) DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def activate_session(
        self,
        session_id: str,
        owner_id: str,
        thread_id: str,
        thread_url: str | None = None,
    ) -> None:
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET provider_thread_id = ?, provider_thread_url = COALESCE(?, provider_thread_url),
                    status = 'active',
                    updated_at = ?, last_active_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (thread_id, thread_url, timestamp, timestamp, session_id, owner_id),
            )
            connection.execute(
                """
                UPDATE tasks SET provider_thread_id = ?,
                    provider_thread_url = COALESCE(?, provider_thread_url),
                    status = 'idle', updated_at = ?
                WHERE session_id = ? AND project_id = ?
                """,
                (thread_id, thread_url, timestamp, session_id, owner_id),
            )

    def set_session_status(self, session_id: str, owner_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ? AND owner_id = ?",
                (status, utc_now(), session_id, owner_id),
            )

    def delete_session(self, session_id: str, owner_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE id = ? AND owner_id = ?",
                (session_id, owner_id),
            )
        return cursor.rowcount > 0

    def add_message(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        message_id = f"msg_{uuid.uuid4().hex}"
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (message_id, session_id, role, content, timestamp),
            )
        return {
            "id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": timestamp,
        }

    def list_messages(self, session_id: str, owner_id: str) -> list[dict[str, Any]]:
        if not self.get_session(session_id, owner_id):
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_file(
        self,
        *,
        owner_id: str,
        session_id: str | None,
        source: str,
        original_name: str,
        stored_path: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
    ) -> dict[str, Any]:
        file_id = f"file_{uuid.uuid4().hex}"
        timestamp = utc_now()
        expiry = expires_after(Config.FILE_TTL_HOURS)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO files
                    (id, owner_id, session_id, source, original_name, stored_path,
                     mime_type, size_bytes, sha256, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id, owner_id, session_id, source, original_name, stored_path,
                    mime_type, size_bytes, sha256, timestamp, expiry,
                ),
            )
        return self.get_file(file_id, owner_id)  # type: ignore[return-value]

    def create_file_checked(
        self,
        *,
        owner_id: str,
        session_id: str | None,
        source: str,
        original_name: str,
        stored_path: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        quota_bytes: int,
        max_session_files: int,
    ) -> dict[str, Any]:
        """Atomically check quota/count and insert a READY file row."""
        file_id = f"file_{uuid.uuid4().hex}"
        timestamp = utc_now()
        expiry = expires_after(Config.FILE_TTL_HOURS)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            usage = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM files "
                "WHERE owner_id = ? AND expires_at >= ? AND status != 'DELETED'",
                (owner_id, timestamp),
            ).fetchone()
            if int(usage["total"] if usage else 0) + size_bytes > quota_bytes:
                raise StorageLimitError("FILE_QUOTA_EXCEEDED", "File storage quota exceeded")
            if session_id:
                count = connection.execute(
                    "SELECT COUNT(*) AS total FROM files "
                    "WHERE owner_id = ? AND session_id = ? AND expires_at >= ? AND status != 'DELETED'",
                    (owner_id, session_id, timestamp),
                ).fetchone()
                if int(count["total"] if count else 0) + 1 > max_session_files:
                    raise StorageLimitError("SESSION_FILE_LIMIT", "Session file count limit exceeded")
            connection.execute(
                """
                INSERT INTO files
                    (id, owner_id, session_id, source, original_name, stored_path,
                     mime_type, size_bytes, sha256, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY')
                """,
                (
                    file_id, owner_id, session_id, source, original_name, stored_path,
                    mime_type, size_bytes, sha256, timestamp, expiry,
                ),
            )
        return self.get_file(file_id, owner_id)  # type: ignore[return-value]

    def get_file(self, file_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM files WHERE id = ?"
        params: tuple[Any, ...] = (file_id,)
        if owner_id is not None:
            query += " AND owner_id = ?"
            params = (file_id, owner_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._row(row)

    def list_files(self, owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM files WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_file(self, file_id: str, owner_id: str) -> dict[str, Any] | None:
        record = self.get_file(file_id, owner_id)
        if not record:
            return None
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM files WHERE id = ? AND owner_id = ?",
                (file_id, owner_id),
            )
        return record

    def set_file_status(
        self,
        file_id: str,
        owner_id: str,
        status: str,
        error_message: str | None = None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE files SET status = ?, error_message = ? WHERE id = ? AND owner_id = ?",
                (status, error_message, file_id, owner_id),
            )
        return cursor.rowcount > 0

    def expired_files(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM files WHERE expires_at < ?",
                (utc_now(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def owner_usage(self, owner_id: str) -> dict[str, int]:
        """Live (non-expired) storage usage for one owner."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes,
                       COUNT(*) AS file_count
                FROM files
                WHERE owner_id = ? AND expires_at >= ?
                """,
                (owner_id, utc_now()),
            ).fetchone()
        return {
            "total_bytes": int(row["total_bytes"]) if row else 0,
            "file_count": int(row["file_count"]) if row else 0,
        }

    def session_file_count(self, session_id: str, owner_id: str) -> int:
        """Live (non-expired) file count bound to one session."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM files
                WHERE session_id = ? AND owner_id = ? AND expires_at >= ?
                """,
                (session_id, owner_id, utc_now()),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def system_summary(self) -> dict[str, int]:
        """Return aggregate counters for the operations dashboard."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sessions) AS sessions,
                    (SELECT COUNT(*) FROM messages) AS messages,
                    (SELECT COUNT(*) FROM files WHERE expires_at >= ?) AS files,
                    (SELECT COALESCE(SUM(size_bytes), 0)
                       FROM files WHERE expires_at >= ?) AS file_bytes
                """,
                (utc_now(), utc_now()),
            ).fetchone()
        return {
            "sessions": int(row["sessions"]) if row else 0,
            "messages": int(row["messages"]) if row else 0,
            "files": int(row["files"]) if row else 0,
            "file_bytes": int(row["file_bytes"]) if row else 0,
        }

    @staticmethod
    def _project_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload.pop("token_hash", None)
        payload["token_available"] = bool(payload.pop("token_ciphertext", None))
        payload["multi_agent"] = bool(payload.get("multi_agent"))
        payload["enabled"] = bool(payload.get("enabled"))
        return payload

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_project_token() -> str:
        return f"cgpt_{secrets.token_urlsafe(32)}"

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        description: str = "",
        multi_agent: bool = False,
    ) -> tuple[dict[str, Any], str]:
        token = self._new_project_token()
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects
                    (project_id, name, description, token_hash, token_ciphertext, token_prefix,
                     multi_agent, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    project_id,
                    name,
                    description,
                    self._token_hash(token),
                    protect_token(token),
                    token[:13],
                    int(multi_agent),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_payload(row), token  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM sessions s
                         WHERE s.owner_id = p.project_id) AS session_count,
                       (SELECT COUNT(*) FROM messages m
                         JOIN sessions s ON s.id = m.session_id
                         WHERE s.owner_id = p.project_id) AS message_count
                FROM projects p
                WHERE p.deleted_at IS NULL
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        return [self._project_payload(row) for row in rows]  # type: ignore[misc]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
        return self._project_payload(row)

    def project_exists(self, project_id: str) -> bool:
        """Include deleted tombstones so a project ID cannot inherit old sessions."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return row is not None

    def get_project_token(self, project_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT token_ciphertext FROM projects
                WHERE project_id = ? AND deleted_at IS NULL
                """,
                (project_id,),
            ).fetchone()
        if row is None or not row["token_ciphertext"]:
            return None
        return unprotect_token(row["token_ciphertext"])

    def resolve_project_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM projects
                WHERE token_hash = ? AND enabled = 1 AND deleted_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE projects SET last_used_at = ? WHERE project_id = ?",
                    (utc_now(), row["project_id"]),
                )
        return self._project_payload(row)

    def rotate_project_token(self, project_id: str) -> tuple[dict[str, Any], str] | None:
        token = self._new_project_token()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE projects
                SET token_hash = ?, token_ciphertext = ?, token_prefix = ?,
                    enabled = 1, updated_at = ?
                WHERE project_id = ? AND deleted_at IS NULL
                """,
                (
                    self._token_hash(token),
                    protect_token(token),
                    token[:13],
                    utc_now(),
                    project_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_payload(row), token  # type: ignore[return-value]

    def set_project_enabled(self, project_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE projects SET enabled = ?, updated_at = ?
                WHERE project_id = ? AND deleted_at IS NULL
                """,
                (int(enabled), utc_now(), project_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_payload(row)

    def delete_project(self, project_id: str) -> dict[str, Any] | None:
        """Revoke and hide a project while retaining its session/file history."""
        project = self.get_project(project_id)
        if project is None:
            return None
        timestamp = utc_now()
        tombstone_hash = self._token_hash(f"deleted:{project_id}:{secrets.token_urlsafe(32)}")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE projects
                SET token_hash = ?, token_ciphertext = NULL, enabled = 0,
                    deleted_at = ?, updated_at = ?
                WHERE project_id = ? AND deleted_at IS NULL
                """,
                (tombstone_hash, timestamp, timestamp, project_id),
            )
            if cursor.rowcount == 0:
                return None
        project["enabled"] = False
        project["token_available"] = False
        project["deleted_at"] = timestamp
        return project

    def project_count(self, *, enabled_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS count FROM projects WHERE deleted_at IS NULL"
        if enabled_only:
            query += " AND enabled = 1"
        with self._lock, self._connect() as connection:
            row = connection.execute(query).fetchone()
        return int(row["count"]) if row else 0

    # ── Agents and capabilities ───────────────────────────────

    def create_agent(
        self,
        project_id: str,
        agent_id: str,
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents(project_id, id, name, description, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (project_id, agent_id, name, description, timestamp, timestamp),
            )
        return self.get_agent(project_id, agent_id)  # type: ignore[return-value]

    def get_agent(self, project_id: str, agent_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE project_id = ? AND id = ?",
                (project_id, agent_id),
            ).fetchone()
        payload = self._row(row)
        if payload:
            payload["enabled"] = bool(payload["enabled"])
        return payload

    def list_agents(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agents WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(row, enabled=bool(row["enabled"])) for row in rows]

    def update_agent(
        self,
        project_id: str,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_agent(project_id, agent_id)
        if not current:
            return None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agents SET name = ?, description = ?, enabled = ?, updated_at = ?
                WHERE project_id = ? AND id = ?
                """,
                (
                    name if name is not None else current["name"],
                    description if description is not None else current["description"],
                    int(enabled if enabled is not None else current["enabled"]),
                    utc_now(), project_id, agent_id,
                ),
            )
        return self.get_agent(project_id, agent_id)

    def disable_agent(self, project_id: str, agent_id: str) -> bool:
        return self.update_agent(project_id, agent_id, enabled=False) is not None

    def set_agent_capabilities(
        self,
        project_id: str,
        agent_id: str,
        capabilities: set[str],
    ) -> list[str]:
        if not self.get_agent(project_id, agent_id):
            raise KeyError(agent_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_capabilities WHERE project_id = ? AND agent_id = ?",
                (project_id, agent_id),
            )
            timestamp = utc_now()
            connection.executemany(
                """
                INSERT INTO agent_capabilities(project_id, agent_id, capability, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(project_id, agent_id, item, timestamp) for item in sorted(capabilities)],
            )
        return self.get_agent_capabilities(project_id, agent_id)

    def get_agent_capabilities(self, project_id: str, agent_id: str) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT capability FROM agent_capabilities
                WHERE project_id = ? AND agent_id = ? ORDER BY capability
                """,
                (project_id, agent_id),
            ).fetchall()
        return [str(row["capability"]) for row in rows]

    # ── Task conversations ────────────────────────────────────

    def create_task(
        self,
        project_id: str,
        agent_id: str,
        name: str,
        provider: str,
    ) -> dict[str, Any]:
        agent = self.get_agent(project_id, agent_id)
        if not agent or not agent["enabled"]:
            raise KeyError(agent_id)
        timestamp = utc_now()
        session_id = f"ses_{uuid.uuid4().hex}"
        with self._lock, self._connect() as connection:
            for _ in range(10):
                task_id = f"tsk_{secrets.token_hex(4).upper()}"
                exists = connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if not exists:
                    break
            else:
                raise RuntimeError("Could not allocate a unique task id")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions
                    (id, owner_id, provider, title, status, created_at, updated_at, agent_id, task_id)
                VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?)
                """,
                (session_id, project_id, provider, task_id, timestamp, timestamp, agent_id, task_id),
            )
            connection.execute(
                """
                INSERT INTO tasks
                    (task_id, project_id, agent_id, session_id, provider_title, name,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
                """,
                (task_id, project_id, agent_id, session_id, task_id, name, timestamp, timestamp),
            )
        return self.get_task(project_id, task_id)  # type: ignore[return-value]

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE project_id = ? AND task_id = ?",
                (project_id, task_id),
            ).fetchone()
        return self._row(row)

    def list_tasks(
        self,
        project_id: str,
        *,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks WHERE project_id = ?"
        params: list[Any] = [project_id]
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def set_task_status(
        self,
        project_id: str,
        task_id: str,
        status: str,
        *,
        started: bool = False,
    ) -> None:
        timestamp = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks SET status = ?, updated_at = ?,
                    last_started_at = CASE WHEN ? THEN ? ELSE last_started_at END
                WHERE project_id = ? AND task_id = ?
                """,
                (status, timestamp, int(started), timestamp, project_id, task_id),
            )

    # ── Idempotent request ledger ─────────────────────────────

    def create_request(
        self,
        *,
        project_id: str,
        agent_id: str | None,
        task_id: str | None,
        session_id: str,
        content_hash: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = utc_now()
        request_id = f"req_{uuid.uuid4().hex}"
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = connection.execute(
                    """
                    SELECT * FROM requests
                    WHERE project_id = ? AND agent_id IS ? AND idempotency_key = ?
                    """,
                    (project_id, agent_id, idempotency_key),
                ).fetchone()
                if row:
                    existing = dict(row)
                    if existing["content_hash"] != content_hash:
                        raise StorageLimitError(
                            "IDEMPOTENCY_CONFLICT",
                            "Idempotency key was already used for a different request",
                        )
                    return self._request_payload(existing), False
            connection.execute(
                """
                INSERT INTO requests
                    (id, project_id, agent_id, task_id, session_id, idempotency_key,
                     content_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    request_id, project_id, agent_id, task_id, session_id, idempotency_key,
                    content_hash, timestamp, timestamp,
                ),
            )
        return self.get_request(project_id, request_id), True  # type: ignore[return-value]

    @staticmethod
    def _request_payload(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        raw = payload.pop("result_json", None)
        payload["result"] = json.loads(raw) if raw else None
        return payload

    def get_request(self, project_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE project_id = ? AND id = ?",
                (project_id, request_id),
            ).fetchone()
        return self._request_payload(row) if row else None

    def update_request(
        self,
        request_id: str,
        status: str,
        *,
        provider_thread_id: str | None = None,
        provider_thread_url: str | None = None,
        result: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        timestamp = utc_now()
        sent_at = timestamp if status in {"sending", "sent", "waiting_response"} else None
        completed_at = timestamp if status in {"completed", "failed", "cancelled", "unknown"} else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requests SET status = ?,
                    provider_thread_id = COALESCE(?, provider_thread_id),
                    provider_thread_url = COALESCE(?, provider_thread_url),
                    result_json = COALESCE(?, result_json), error_type = ?,
                    sent_at = COALESCE(sent_at, ?), completed_at = COALESCE(?, completed_at),
                    updated_at = ? WHERE id = ?
                """,
                (
                    status, provider_thread_id, provider_thread_url,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_type, sent_at, completed_at, timestamp, request_id,
                ),
            )
