"""Single governed entry point for uploaded, inline and remote files."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import Config
from src.files.remote_fetch import RemoteFetchError, fetch_remote_file
from src.storage.database import Database, StorageLimitError


class FileServiceError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _safe_name(filename: str) -> str:
    original = Path(filename or "attachment.bin").name
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in original)
    return safe[:180] or "attachment.bin"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class FileService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def max_bytes(self) -> int:
        return Config.MAX_FILE_SIZE_MB * 1024 * 1024

    def _validate_session(self, owner_id: str, session_id: str | None) -> None:
        if session_id and not self.database.get_session(session_id, owner_id):
            raise FileServiceError("FILE_SESSION_NOT_FOUND", "Session not found", 404)

    def _validate_quota(self, owner_id: str, extra_bytes: int, session_id: str | None) -> None:
        usage = self.database.owner_usage(owner_id)
        if usage["total_bytes"] + extra_bytes > Config.USER_FILE_QUOTA_MB * 1024 * 1024:
            raise FileServiceError("FILE_QUOTA_EXCEEDED", "File storage quota exceeded", 507)
        if session_id and self.database.session_file_count(session_id, owner_id) + 1 > Config.MAX_FILES_PER_SESSION:
            raise FileServiceError("SESSION_FILE_LIMIT", "Session file count limit exceeded", 409)

    def _store_path(self, owner_id: str, filename: str) -> Path:
        owner = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in owner_id)[:80] or "owner"
        extension = Path(filename).suffix.lower()[:12]
        key = uuid.uuid4().hex
        directory = Config.FILES_DIR / owner / key
        directory.mkdir(parents=True, exist_ok=False)
        return directory / f"{uuid.uuid4().hex}{extension}"

    def _register_path(
        self,
        path: Path,
        *,
        owner_id: str,
        session_id: str | None,
        source: str,
        original_name: str,
        mime_type: str,
    ) -> dict[str, Any]:
        self._validate_session(owner_id, session_id)
        size = path.stat().st_size
        if size > self.max_bytes:
            raise FileServiceError("FILE_TOO_LARGE", f"File exceeds {Config.MAX_FILE_SIZE_MB} MB limit", 413)
        self._validate_quota(owner_id, size, session_id)
        target = self._store_path(owner_id, original_name)
        try:
            shutil.move(str(path), str(target))
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            try:
                return self.database.create_file_checked(
                    owner_id=owner_id,
                    session_id=session_id,
                    source=source,
                    original_name=Path(original_name).name,
                    stored_path=str(target.resolve()),
                    mime_type=mime_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                    quota_bytes=Config.USER_FILE_QUOTA_MB * 1024 * 1024,
                    max_session_files=Config.MAX_FILES_PER_SESSION,
                )
            except StorageLimitError as error:
                status = 507 if error.code == "FILE_QUOTA_EXCEEDED" else 409
                raise FileServiceError(error.code, str(error), status) from error
        except Exception:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass
            raise

    async def create_from_upload(
        self,
        upload,
        *,
        owner_id: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        self._validate_session(owner_id, session_id)
        Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp = Config.TEMP_DIR / f"upload_{uuid.uuid4().hex}.part"
        total = 0
        content_type = upload.content_type or "application/octet-stream"
        original_name = Path(upload.filename or "upload.bin").name
        try:
            with temp.open("xb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise FileServiceError(
                            "FILE_TOO_LARGE",
                            f"File exceeds {Config.MAX_FILE_SIZE_MB} MB limit",
                            413,
                        )
                    output.write(chunk)
            return self._register_path(
                temp,
                owner_id=owner_id,
                session_id=session_id,
                source="upload",
                original_name=original_name,
                mime_type=content_type,
            )
        finally:
            temp.unlink(missing_ok=True)
            await upload.close()

    async def create_from_base64(
        self,
        data: str,
        *,
        owner_id: str,
        session_id: str | None,
        filename: str = "attachment.bin",
        mime_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        if data.startswith("data:"):
            try:
                header, data = data.split(",", 1)
            except ValueError as error:
                raise FileServiceError("INVALID_BASE64", "Invalid data URL") from error
            if ";base64" not in header.lower():
                raise FileServiceError("INVALID_BASE64", "Only base64 data URLs are allowed")
            declared = header[5:].split(";", 1)[0].strip()
            if declared:
                mime_type = declared
            if filename == "attachment.bin":
                extension = mimetypes.guess_extension(mime_type) or ".bin"
                filename = f"attachment{extension}"
        compact = "".join(data.split())
        if len(compact) > ((self.max_bytes + 2) // 3) * 4 + 4:
            raise FileServiceError("FILE_TOO_LARGE", "Base64 attachment exceeds size limit", 413)
        try:
            payload = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as error:
            raise FileServiceError("INVALID_BASE64", "Attachment is not valid base64") from error
        if len(payload) > self.max_bytes:
            raise FileServiceError("FILE_TOO_LARGE", "Base64 attachment exceeds size limit", 413)
        Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp = Config.TEMP_DIR / f"base64_{uuid.uuid4().hex}.part"
        try:
            temp.write_bytes(payload)
            return self._register_path(
                temp,
                owner_id=owner_id,
                session_id=session_id,
                source="base64",
                original_name=_safe_name(filename),
                mime_type=mime_type,
            )
        finally:
            temp.unlink(missing_ok=True)

    async def create_from_remote_url(
        self,
        url: str,
        *,
        owner_id: str,
        session_id: str | None,
    ) -> dict[str, Any]:
        self._validate_session(owner_id, session_id)
        try:
            remote = await asyncio.to_thread(
                fetch_remote_file,
                url,
                Config.TEMP_DIR,
                self.max_bytes,
                connect_timeout=Config.REMOTE_FILE_CONNECT_TIMEOUT_SECONDS,
                read_timeout=Config.REMOTE_FILE_READ_TIMEOUT_SECONDS,
            )
        except RemoteFetchError as error:
            raise FileServiceError("REMOTE_FILE_REJECTED", str(error), 400) from error
        try:
            return self._register_path(
                remote.path,
                owner_id=owner_id,
                session_id=session_id,
                source="remote_url",
                original_name=remote.filename,
                mime_type=remote.mime_type,
            )
        finally:
            remote.path.unlink(missing_ok=True)

    def register_generated(
        self,
        path: Path,
        *,
        owner_id: str,
        session_id: str | None,
        original_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Move a provider-generated artifact into governed storage."""
        source = Path(path).resolve()
        if not source.is_file():
            raise FileServiceError("FILE_NOT_FOUND", "Generated file is unavailable", 404)
        return self._register_path(
            source,
            owner_id=owner_id,
            session_id=session_id,
            source="generated",
            original_name=original_name or source.name,
            mime_type=mime_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        )

    def resolve_for_session(
        self,
        file_id: str,
        *,
        owner_id: str,
        session_id: str | None,
        require_session_match: bool = True,
    ) -> dict[str, Any]:
        record = self.database.get_file(file_id, owner_id)
        if not record:
            raise FileServiceError("FILE_NOT_FOUND", f"File not found: {file_id}", 404)
        if require_session_match and session_id and record.get("session_id") != session_id:
            raise FileServiceError("FILE_SESSION_MISMATCH", f"File belongs to a different session: {file_id}", 409)
        if datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
            raise FileServiceError("FILE_EXPIRED", f"File expired: {file_id}", 410)
        path = Path(record["stored_path"]).resolve()
        root = Config.FILES_DIR.resolve()
        if not _inside(path, root) or not path.is_file():
            raise FileServiceError("FILE_EXPIRED", f"File content is unavailable: {file_id}", 410)
        return record

    async def create_from_source(
        self,
        value: str | dict[str, Any],
        *,
        owner_id: str,
        session_id: str | None,
        require_session_match: bool = True,
    ) -> dict[str, Any]:
        if isinstance(value, dict):
            file_id = str(value.get("file_id") or "").strip()
            if file_id:
                return self.resolve_for_session(
                    file_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    require_session_match=require_session_match,
                )
            remote_url = str(value.get("url") or "").strip()
            data = value.get("data_b64") or value.get("data")
            if data:
                return await self.create_from_base64(
                    str(data),
                    owner_id=owner_id,
                    session_id=session_id,
                    filename=str(value.get("filename") or "attachment.bin"),
                    mime_type=str(value.get("mime_type") or "application/octet-stream"),
                )
            if remote_url:
                value = remote_url
            else:
                raise FileServiceError("INVALID_FILE_SOURCE", "Attachment must contain file_id, base64 data, or an HTTPS URL")
        source = str(value).strip()
        if source.startswith("file_"):
            return self.resolve_for_session(
                source,
                owner_id=owner_id,
                session_id=session_id,
                require_session_match=require_session_match,
            )
        if source.startswith("data:"):
            return await self.create_from_base64(
                source,
                owner_id=owner_id,
                session_id=session_id,
            )
        if source.lower().startswith("https://"):
            return await self.create_from_remote_url(source, owner_id=owner_id, session_id=session_id)
        raise FileServiceError(
            "LOCAL_PATH_FORBIDDEN",
            "Local paths and non-HTTPS attachment URLs are not allowed; use file_id, HTTPS, or base64",
            400,
        )

    def delete(self, file_id: str, *, owner_id: str) -> bool:
        record = self.database.get_file(file_id, owner_id)
        if not record:
            return False
        path = Path(record["stored_path"]).resolve()
        root = Config.FILES_DIR.resolve()
        if not _inside(path, root):
            raise FileServiceError("INVALID_STORED_PATH", "Managed file path escaped storage root", 500)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            self.database.set_file_status(file_id, owner_id, "DELETE_PENDING", str(error)[:500])
            raise FileServiceError("FILE_DELETE_FAILED", "Could not delete file content", 503) from error
        self.database.delete_file(file_id, owner_id)
        return True
