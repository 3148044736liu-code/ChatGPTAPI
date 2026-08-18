"""Focused safety checks for the Phase 1 attachment and auth boundaries."""

from __future__ import annotations

import base64
import socket

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.config import Config
from src.files.remote_fetch import RemoteFetchError, validate_remote_url
from src.files.service import FileService, FileServiceError


def _resolver(address: str):
    def resolve(host, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return resolve


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Windows\System32\drivers\etc\hosts",
        "/etc/passwd",
        "../.env",
        "http://127.0.0.1/private.txt",
    ],
)
@pytest.mark.asyncio
async def test_file_service_rejects_local_and_insecure_sources(database, value):
    service = FileService(database)
    with pytest.raises(FileServiceError) as captured:
        await service.create_from_source(
            value,
            owner_id="alice",
            session_id=None,
            require_session_match=False,
        )
    assert captured.value.code == "LOCAL_PATH_FORBIDDEN"


@pytest.mark.asyncio
async def test_base64_attachment_is_managed(database):
    service = FileService(database)
    payload = base64.b64encode(b"managed attachment").decode()
    record = await service.create_from_source(
        {"filename": "note.txt", "mime_type": "text/plain", "data_b64": payload},
        owner_id="alice",
        session_id=None,
        require_session_match=False,
    )
    assert record["source"] == "base64"
    assert database.get_file(record["id"], "alice") is not None
    assert Config.FILES_DIR.resolve() in __import__("pathlib").Path(record["stored_path"]).resolve().parents


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.1.2", "192.168.10.1", "169.254.1.1"],
)
def test_remote_url_rejects_non_public_dns(address):
    with pytest.raises(RemoteFetchError, match="non-public"):
        validate_remote_url("https://files.example/test.pdf", _resolver(address))


def test_remote_url_requires_https():
    with pytest.raises(RemoteFetchError, match="https"):
        validate_remote_url("http://files.example/test.pdf", _resolver("93.184.216.34"))


def test_business_api_fails_closed_without_any_token(monkeypatch, database):
    from src.api import server as server_module

    monkeypatch.setattr(Config, "API_TOKEN", "")
    monkeypatch.setattr(Config, "API_USER_TOKENS", "")
    monkeypatch.setattr(server_module, "_database", database)
    app = FastAPI()
    app.add_middleware(server_module.BearerTokenMiddleware)

    @app.get("/private")
    async def private():
        return {"ok": True}

    response = TestClient(app).get("/private")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_not_configured"


def _admin_request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/api/dashboard/projects",
            "raw_path": b"/api/dashboard/projects",
            "query_string": b"",
            "headers": [(b"x-admin-token", token.encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 80),
        }
    )


def test_dashboard_requires_dedicated_admin_token():
    from src.api.dashboard import _require_admin

    _require_admin(_admin_request(Config.ADMIN_TOKEN))
    with pytest.raises(HTTPException) as captured:
        _require_admin(_admin_request("wrong-token"))
    assert captured.value.status_code == 401


def test_dashboard_can_disable_token_auth_on_private_network(monkeypatch):
    from src.api.dashboard import _require_admin

    monkeypatch.setattr(Config, "DASHBOARD_REQUIRE_ADMIN_TOKEN", False)
    _require_admin(_admin_request(""))
