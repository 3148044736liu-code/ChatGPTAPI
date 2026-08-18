"""SSRF-safe streaming downloads for managed remote attachments."""

from __future__ import annotations

import http.client
import ipaddress
import mimetypes
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit


class RemoteFetchError(ValueError):
    """A remote attachment was unsafe or could not be downloaded."""


@dataclass(frozen=True)
class RemoteFile:
    path: Path
    filename: str
    mime_type: str
    size_bytes: int


Resolver = Callable[..., list[tuple]]
_REDIRECTS = {301, 302, 303, 307, 308}
_ALLOWED_EXTENSIONS = {
    ".bmp", ".csv", ".doc", ".docx", ".gif", ".heic", ".jpeg", ".jpg",
    ".json", ".md", ".pdf", ".png", ".ppt", ".pptx", ".svg", ".tif",
    ".tiff", ".txt", ".webp", ".xls", ".xlsx", ".zip",
}


def _resolved_public_addresses(hostname: str, port: int, resolver: Resolver) -> list[str]:
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RemoteFetchError("Remote host could not be resolved") from error
    addresses: list[str] = []
    for record in records:
        candidate = record[4][0]
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as error:
            raise RemoteFetchError("Remote host resolved to an invalid address") from error
        if not address.is_global:
            raise RemoteFetchError("Remote URL resolves to a non-public IP address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise RemoteFetchError("Remote host did not resolve to an address")
    return addresses


def validate_remote_url(url: str, resolver: Resolver = socket.getaddrinfo) -> tuple[str, int, list[str]]:
    """Validate one URL hop and return hostname, port and all resolved IPs."""
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as error:
        raise RemoteFetchError("Invalid remote URL") from error
    if parsed.scheme.lower() != "https":
        raise RemoteFetchError("Only https:// remote attachments are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise RemoteFetchError("Remote URL must contain a host and no credentials")
    host = parsed.hostname.rstrip(".")
    addresses = _resolved_public_addresses(host, port, resolver)
    return host, port, addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose TCP peer is a previously validated DNS result."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._validated_address = address

    def _create_connection(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        return socket.create_connection(
            (self._validated_address, self.port),
            timeout,
            source_address,
        )


def _safe_filename(url: str, content_disposition: str | None) -> str:
    candidate = ""
    if content_disposition:
        for part in content_disposition.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() in {"filename", "filename*"}:
                candidate = value.strip().strip('"').split("''")[-1]
                break
    if not candidate:
        candidate = Path(unquote(urlsplit(url).path)).name
    candidate = Path(candidate or "remote.bin").name
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in candidate)
    return safe[:180] or "remote.bin"


def _validate_content_type(filename: str, header_value: str | None) -> str:
    mime_type = (header_value or "application/octet-stream").split(";", 1)[0].strip().lower()
    extension = Path(filename).suffix.lower()
    if extension and extension not in _ALLOWED_EXTENSIONS:
        raise RemoteFetchError(f"Remote file extension is not allowed: {extension}")
    guessed = (mimetypes.guess_type(filename)[0] or "").lower()
    if guessed and mime_type not in {"", "application/octet-stream"}:
        guessed_family = guessed.split("/", 1)[0]
        actual_family = mime_type.split("/", 1)[0]
        office_types = {
            "application/msword",
            "application/vnd.ms-excel",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        if guessed != mime_type and not (
            guessed_family == actual_family == "image"
            or guessed in office_types and mime_type in office_types
            or guessed.startswith("text/") and mime_type.startswith("text/")
        ):
            raise RemoteFetchError("Remote file MIME type does not match its extension")
    return mime_type or guessed or "application/octet-stream"


def fetch_remote_file(
    url: str,
    destination_dir: Path,
    max_bytes: int,
    *,
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
    max_redirects: int = 5,
    resolver: Resolver = socket.getaddrinfo,
) -> RemoteFile:
    """Download an HTTPS attachment while validating DNS and every redirect."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    current_url = url
    for hop in range(max_redirects + 1):
        host, port, addresses = validate_remote_url(current_url, resolver)
        parsed = urlsplit(current_url)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        response = None
        connection = None
        last_error: Exception | None = None
        for address in addresses:
            try:
                connection = _PinnedHTTPSConnection(host, port, address, connect_timeout)
                connection.request(
                    "GET",
                    target,
                    headers={"Host": host, "User-Agent": "GPT-FastAPI/2 remote-fetch", "Accept": "*/*"},
                )
                response = connection.getresponse()
                if connection.sock is not None:
                    connection.sock.settimeout(read_timeout)
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as error:
                last_error = error
                if connection is not None:
                    connection.close()
                connection = None
        if response is None or connection is None:
            raise RemoteFetchError("Remote attachment connection failed") from last_error
        try:
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                if not location:
                    raise RemoteFetchError("Remote redirect has no Location header")
                if hop >= max_redirects:
                    raise RemoteFetchError("Remote attachment exceeded redirect limit")
                current_url = urljoin(current_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise RemoteFetchError(f"Remote attachment returned HTTP {response.status}")
            length_header = response.getheader("Content-Length")
            if length_header:
                try:
                    if int(length_header) > max_bytes:
                        raise RemoteFetchError("Remote attachment exceeds the configured size limit")
                except ValueError as error:
                    raise RemoteFetchError("Remote attachment has an invalid Content-Length") from error
            filename = _safe_filename(current_url, response.getheader("Content-Disposition"))
            mime_type = _validate_content_type(filename, response.getheader("Content-Type"))
            descriptor, temp_name = tempfile.mkstemp(prefix="remote_", suffix=".part", dir=destination_dir)
            total = 0
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise RemoteFetchError("Remote attachment exceeds the configured size limit")
                        output.write(chunk)
                return RemoteFile(Path(temp_name), filename, mime_type, total)
            except Exception:
                Path(temp_name).unlink(missing_ok=True)
                raise
        finally:
            response.close()
            connection.close()
    raise RemoteFetchError("Remote attachment exceeded redirect limit")
