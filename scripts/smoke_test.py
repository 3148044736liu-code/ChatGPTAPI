"""Smoke test for a RUNNING CatGPT-Gateway service.

Exercises every endpoint group against the live server:
  - system: /healthz, /docs, /openapi.json
  - auth:   missing / invalid / valid Bearer tokens
  - managed sessions & files (CRUD, upload/download, signed URLs, limits)
  - OpenAI-compatible endpoints (models, chat completions via session pool)
  - legacy endpoints (/status, /threads)

Usage:
  .venv/Scripts/python.exe scripts/smoke_test.py [--base URL] [--token TOKEN]
      [--token2 TOKEN] [--no-live]

--token2 enables multi-user isolation checks.
--no-live skips the two checks that send real messages to ChatGPT.

Exit code: 0 when all checks pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import sys
import uuid

# Keep output stable regardless of console codepage.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx

DEFAULT_BASE = "http://192.168.8.222:5061"
DEFAULT_TOKEN = "dummy123"
TIMEOUT = 15.0
LIVE_TIMEOUT = 240.0


class Reporter:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []  # (status, name, detail)

    def record(self, status: str, name: str, detail: str = "") -> None:
        self.results.append((status, name, detail))
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
        line = f"{mark} {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    def ok(self, name: str, detail: str = "") -> None:
        self.record("PASS", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.record("FAIL", name, detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.record("SKIP", name, detail)

    def summary(self) -> int:
        passed = sum(1 for r in self.results if r[0] == "PASS")
        failed = sum(1 for r in self.results if r[0] == "FAIL")
        skipped = sum(1 for r in self.results if r[0] == "SKIP")
        print(f"\n== Summary: {passed} passed, {failed} failed, {skipped} skipped ==")
        return 1 if failed else 0


def check(rep: Reporter, name: str, fn) -> None:
    try:
        detail = fn() or ""
        rep.ok(name, str(detail))
    except AssertionError as e:
        rep.fail(name, str(e) or "assertion failed")
    except Exception as e:  # noqa: BLE001 — report, don't crash the run
        rep.fail(name, f"{type(e).__name__}: {e}")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="CatGPT-Gateway smoke test")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--token2", default=None, help="second user token for isolation checks")
    parser.add_argument("--no-live", action="store_true", help="skip real ChatGPT message checks")
    args = parser.parse_args()

    rep = Reporter()
    client = httpx.Client(base_url=args.base, timeout=TIMEOUT)
    live_client = httpx.Client(base_url=args.base, timeout=LIVE_TIMEOUT)
    H = auth(args.token)

    print(f"Smoke test target: {args.base}\n")

    # ── System endpoints ────────────────────────────────────────
    def c_healthz():
        r = client.get("/healthz")
        assert r.status_code == 200, f"status {r.status_code}"
        assert r.json().get("status") == "ok", r.text
        return "status ok"

    def c_docs():
        r = client.get("/docs")
        assert r.status_code == 200, f"status {r.status_code}"
        assert "swagger" in r.text.lower(), "docs page unexpected"
        return "swagger ui served"

    def c_openapi():
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        schemes = spec.get("components", {}).get("securitySchemes", {})
        assert "HTTPBearer" in schemes, "HTTPBearer security scheme missing"
        n_paths = len(spec.get("paths", {}))
        return f"{n_paths} paths, HTTPBearer scheme present"

    check(rep, "系统: GET /healthz", c_healthz)
    check(rep, "系统: GET /docs", c_docs)
    check(rep, "系统: GET /openapi.json (Bearer 方案)", c_openapi)

    # ── Auth ────────────────────────────────────────────────────
    def c_no_token():
        r = client.get("/v1/me")
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        return "401 as expected"

    def c_bad_token():
        r = client.get("/v1/me", headers=auth("definitely-wrong-token"))
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        return "401 as expected"

    def c_valid_token():
        r = client.get("/v1/me", headers=H)
        assert r.status_code == 200, f"status {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("owner_id"), "owner_id missing"
        assert "file_usage" in body, "file_usage missing"
        return f"owner={body['owner_id']}, quota={body['file_usage']['quota_bytes']}"

    check(rep, "鉴权: 无 token -> 401", c_no_token)
    check(rep, "鉴权: 错误 token -> 401", c_bad_token)
    check(rep, "鉴权: 有效 token -> /v1/me", c_valid_token)

    # ── Pool ────────────────────────────────────────────────────
    def c_pool():
        r = client.get("/v1/pool/status", headers=H)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["capacity"] >= 1 and 0 <= body["available"] <= body["capacity"]
        return f"capacity={body['capacity']} available={body['available']} busy={body['busy']}"

    check(rep, "会话池: GET /v1/pool/status", c_pool)

    # ── Session lifecycle ───────────────────────────────────────
    state: dict = {}

    def c_session_create():
        r = client.post("/v1/sessions", json={"title": "smoke test"}, headers=H)
        assert r.status_code == 201, f"status {r.status_code}: {r.text}"
        body = r.json()
        assert body["id"].startswith("ses_"), body["id"]
        assert body["status"] == "new"
        state["session"] = body["id"]
        return body["id"]

    def c_session_get():
        r = client.get(f"/v1/sessions/{state['session']}", headers=H)
        assert r.status_code == 200, r.text
        return "session readable"

    def c_session_list():
        r = client.get("/v1/sessions", headers=H)
        assert r.status_code == 200, r.text
        ids = [s["id"] for s in r.json()["data"]]
        assert state["session"] in ids, "created session not in list"
        return f"{len(ids)} session(s) listed"

    check(rep, "会话: POST /v1/sessions 新建", c_session_create)
    check(rep, "会话: GET /v1/sessions/{id}", c_session_get)
    check(rep, "会话: GET /v1/sessions 列表", c_session_list)

    # ── Multi-user isolation (optional) ─────────────────────────
    if args.token2:
        H2 = auth(args.token2)

        def c_iso():
            r = client.get(f"/v1/sessions/{state['session']}", headers=H2)
            assert r.status_code == 404, f"expected 404, got {r.status_code}"
            r2 = client.get("/v1/sessions", headers=H2)
            ids = [s["id"] for s in r2.json()["data"]]
            assert state["session"] not in ids, "session leaked across users"
            return "other token cannot see the session"

        check(rep, "隔离: 其他用户访问我的会话 -> 404", c_iso)
    else:
        rep.skip("隔离: 多用户隔离检查", "未提供 --token2")

    # ── Files: upload / download / signed URL ───────────────────
    payload = f"smoke-{uuid.uuid4().hex}".encode()

    def c_file_upload():
        r = client.post(
            "/v1/files",
            files={"file": ("smoke.txt", payload, "text/plain")},
            data={"session_id": state["session"]},
            headers=H,
        )
        assert r.status_code == 201, f"status {r.status_code}: {r.text}"
        body = r.json()
        assert body["bytes"] == len(payload)
        assert body["download_url"].startswith("http"), body["download_url"]
        state["file"] = body["id"]
        state["download_url"] = body["download_url"]
        return f"{body['id']} ({body['bytes']} bytes)"

    def c_file_download_bearer():
        r = client.get(f"/v1/files/{state['file']}/content", headers=H)
        assert r.status_code == 200, r.text
        assert r.content == payload, "downloaded bytes differ"
        return "bytes match"

    def c_file_download_signed():
        url = state["download_url"].split(str(client.base_url))[-1]
        r = client.get(url)  # no Authorization header
        assert r.status_code == 200, f"status {r.status_code}: {r.text}"
        assert r.content == payload, "signed-download bytes differ"
        return "bytes match (no bearer needed)"

    def c_file_download_tampered():
        url = state["download_url"].replace("signature=", "signature=00")
        path = url.split(str(client.base_url))[-1]
        r = client.get(path)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        return "tampered signature rejected"

    check(rep, "文件: POST /v1/files 上传", c_file_upload)
    check(rep, "文件: Bearer 下载内容一致", c_file_download_bearer)
    check(rep, "文件: 签名链接下载内容一致", c_file_download_signed)
    check(rep, "文件: 篡改签名 -> 401", c_file_download_tampered)

    # ── Error paths ─────────────────────────────────────────────
    def c_404s():
        assert client.get("/v1/sessions/ses_missing", headers=H).status_code == 404
        assert client.get("/v1/files/file_missing", headers=H).status_code == 404
        assert client.delete("/v1/files/file_missing", headers=H).status_code == 404
        return "sessions/files 404 as expected"

    check(rep, "错误路径: 不存在的资源 -> 404", c_404s)

    # ── Live: managed session message ───────────────────────────
    if args.no_live:
        rep.skip("实测: 会话发消息(真实 ChatGPT)", "--no-live")
        rep.skip("实测: OpenAI 会话池(真实 ChatGPT)", "--no-live")
    else:
        marker = f"SMOKE-{uuid.uuid4().hex[:8]}"

        def c_live_message():
            r = live_client.post(
                f"/v1/sessions/{state['session']}/messages",
                json={"content": f"Reply with exactly: {marker}"},
                headers=H,
            )
            assert r.status_code == 200, f"status {r.status_code}: {r.text}"
            body = r.json()
            assert body["message"], "empty reply"
            assert body["provider_thread_id"], "provider_thread_id missing"
            state["thread"] = body["provider_thread_id"]
            # messages persisted: user + assistant
            msgs = client.get(
                f"/v1/sessions/{state['session']}/messages", headers=H
            ).json()["data"]
            assert len(msgs) >= 2, f"expected >=2 messages, got {len(msgs)}"
            roles = [m["role"] for m in msgs]
            assert "user" in roles and "assistant" in roles
            # session activated
            ses = client.get(f"/v1/sessions/{state['session']}", headers=H).json()
            assert ses["provider_thread_id"] == body["provider_thread_id"]
            reply = body["message"][:60].replace("\n", " ")
            return f"thread={body['provider_thread_id'][:8]}... reply={reply!r}"

        check(rep, "实测: POST /v1/sessions/{id}/messages", c_live_message)

        # ── Live: OpenAI session-pool routing ───────────────────
        def c_openai_models():
            r = client.get("/v1/models", headers=H)
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data and data[0]["id"], "no model listed"
            return f"model={data[0]['id']}"

        def c_openai_pool():
            ses = client.post("/v1/sessions", json={"title": "smoke openai"}, headers=H).json()
            r = live_client.post(
                "/v1/chat/completions",
                json={
                    "model": "catgpt-browser",
                    "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
                    "session_id": ses["id"],
                },
                headers=H,
            )
            assert r.status_code == 200, f"status {r.status_code}: {r.text}"
            body = r.json()
            assert body.get("session_id") == ses["id"], "session_id echo mismatch"
            content = body["choices"][0]["message"]["content"] or ""
            assert content, "empty completion"
            msgs = client.get(f"/v1/sessions/{ses['id']}/messages", headers=H).json()["data"]
            assert len(msgs) >= 2, "pool messages not persisted"
            client.delete(f"/v1/sessions/{ses['id']}", headers=H)
            return f"echo={body['session_id'][:12]}... reply={content[:40]!r}"

        def c_openai_unknown_session():
            r = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "session_id": "ses_missing",
                },
                headers=H,
            )
            assert r.status_code == 404, f"expected 404, got {r.status_code}"
            return "unknown session rejected"

        check(rep, "OpenAI: GET /v1/models", c_openai_models)
        check(rep, "OpenAI: 未知 session_id -> 404", c_openai_unknown_session)
        check(rep, "实测: /v1/chat/completions 走会话池", c_openai_pool)

    # ── Legacy endpoints ────────────────────────────────────────
    def c_legacy_status():
        r = client.get("/status", headers=H)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["logged_in"] is True, "browser not logged in"
        return f"logged_in={body['logged_in']}"

    def c_legacy_threads():
        r = client.get("/threads", headers=H)
        assert r.status_code == 200, r.text
        threads = r.json()["threads"]
        return f"{len(threads)} thread(s) in sidebar"

    check(rep, "旧版: GET /status 登录态", c_legacy_status)
    check(rep, "旧版: GET /threads 侧栏对话", c_legacy_threads)

    # ── Cleanup ─────────────────────────────────────────────────
    def c_cleanup():
        assert client.delete(f"/v1/files/{state.get('file', 'file_missing')}", headers=H).status_code == 200
        assert client.delete(f"/v1/sessions/{state.get('session', 'ses_missing')}", headers=H).status_code == 200
        return "smoke session & file removed"

    check(rep, "清理: 删除冒烟数据", c_cleanup)

    client.close()
    live_client.close()
    return rep.summary()


if __name__ == "__main__":
    raise SystemExit(main())
