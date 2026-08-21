"""Unit tests for the generated-file pipeline.

These tests cover three independent surfaces:

  1. Repair helpers — decide whether a follow-up turn is needed
     (``_needs_generated_file_repair`` / ``_generated_file_repair_prompt``)
     and synthesize text-based fallback files
     (``materialize_text_fallback``).

  2. Helpers used by the new DOM-first capture pipeline — filename
     sanitization, sandbox-path extraction, validation, deduplication,
     and the structured ``GeneratedFileError`` codes.

  3. The ``capture_generated_files`` orchestrator itself, exercised
     against a small in-memory mock of the Playwright ``Page`` /
     ``Locator`` objects.  Only the *contracts* the orchestrator relies
     on are mocked; no real browser is launched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.files.generated import (
    GeneratedFileError,
    GeneratedAttachment,
    NetworkFileCapture,
    _download_via_href,
    _filename_from_href,
    _generated_file_repair_prompt,
    _looks_like_attachment,
    _needs_generated_file_repair,
    _safe_name,
    _validate_downloaded_file,
    capture_generated_files,
    deduplicate_files,
    materialize_text_fallback,
)


# ──────────────────────────────────────────────────────────────────────
# Repair helpers (text-fallback flow)
# ──────────────────────────────────────────────────────────────────────


def test_file_reference_without_attachment_requests_one_repair_turn(tmp_path):
    response = "已经整理好了：下载降雨报告.md"
    message = "请生成并下载一份降雨报告"

    assert _needs_generated_file_repair(message, response, [])
    assert not _needs_generated_file_repair(
        message, response, [tmp_path / "降雨报告.md"]
    )

    prompt = _generated_file_repair_prompt(message, response)
    assert "降雨报告.md" in prompt
    assert "真实文件" in prompt
    assert "不要只回复文件名" in prompt
    assert "fenced code block" in prompt


def test_repair_prompt_omits_filename_when_neither_side_hints_one():
    prompt = _generated_file_repair_prompt("请写一个文件", "好的")
    assert "the file" in prompt
    assert "fenced code block" in prompt


def test_materialize_markdown_fallback_from_fenced_response(tmp_path):
    path = materialize_text_fallback(
        "请创建 rainfall-report.md",
        "内容如下：\n```markdown\n# Rainfall report\n\n42 mm\n```",
        tmp_path,
    )

    assert path is not None
    assert path.name == "rainfall-report.md"
    assert path.read_text(encoding="utf-8") == "# Rainfall report\n\n42 mm\n"


def test_binary_file_is_not_materialized_from_text(tmp_path):
    assert materialize_text_fallback(
        "请创建 report.xlsx", "```text\nnot an xlsx\n```", tmp_path
    ) is None


def test_materialize_requires_fenced_code_block(tmp_path):
    assert materialize_text_fallback(
        "请创建 rainfall-report.md", "好的，下面是内容：# Rain", tmp_path
    ) is None


# ──────────────────────────────────────────────────────────────────────
# Filename / sandbox-path helpers
# ──────────────────────────────────────────────────────────────────────


def test_filename_from_href_uses_fn_query_param():
    href = (
        "https://chatgpt.com/backend-api/estuary/content"
        "?id=file_xyz&fn=%E6%8A%A5%E5%91%8A.docx&cd=attachment"
    )
    assert _filename_from_href(href) == "报告.docx"


def test_filename_from_href_uses_sandbox_path():
    href = (
        "/backend-api/conversation/test/interpreter/download"
        "?message_id=msg&sandbox_path=%2Fmnt%2Fdata%2Frainfall-report.md"
    )
    assert _filename_from_href(href) == "rainfall-report.md"


def test_filename_from_href_falls_back_to_path_basename():
    assert _filename_from_href("https://x/y/foo.csv") == "foo.csv"


def test_safe_name_strips_illegal_windows_chars():
    assert _safe_name('a<b>c:d"e/f\\g|h?i*.txt', "fallback") == "a_b_c_d_e_f_g_h_i_.txt"


def test_looks_like_attachment_classifies_supported_extensions():
    assert _looks_like_attachment("https://x/y/a.docx", "")
    assert _looks_like_attachment("/mnt/data/foo.csv", "foo.csv")
    assert not _looks_like_attachment("https://x/y/a.png", "")
    assert not _looks_like_attachment("https://x/y/a.jpg", "image")
    # Unrelated extension is rejected.
    assert not _looks_like_attachment("https://x/y/a.unknownext", "")


# ──────────────────────────────────────────────────────────────────────
# Validation + dedup
# ──────────────────────────────────────────────────────────────────────


def test_validate_rejects_empty_file(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(GeneratedFileError) as info:
        _validate_downloaded_file(empty)
    assert info.value.code == "FILE_EMPTY"


def test_validate_rejects_html_error_page(tmp_path):
    html = tmp_path / "report.docx"
    html.write_bytes(b"<!doctype html><html><body>Access Denied</body></html>")
    with pytest.raises(GeneratedFileError) as info:
        _validate_downloaded_file(html)
    assert info.value.code == "FILE_INVALID"
    # File should be cleaned up so callers don't ship an HTML page.
    assert not html.exists()


def test_validate_rejects_oversized_file(tmp_path):
    big = tmp_path / "huge.bin"
    big.write_bytes(b"x" * (51 * 1024 * 1024))
    with pytest.raises(GeneratedFileError) as info:
        _validate_downloaded_file(big)
    assert info.value.code == "FILE_INVALID"


def test_validate_accepts_well_formed_pdf(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\n...binary...")
    out = _validate_downloaded_file(pdf)
    assert out == pdf


def test_deduplicate_files_keeps_first_occurrence(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"identical")
    b.write_bytes(b"identical")
    kept = deduplicate_files([a, b])
    assert kept == [a]
    assert not b.exists()


def test_deduplicate_files_keeps_distinct_payloads(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"alpha")
    b.write_bytes(b"beta")
    kept = deduplicate_files([a, b])
    assert len(kept) == 2
    assert {p.name for p in kept} == {"a.bin", "b.bin"}


# ──────────────────────────────────────────────────────────────────────
# Mock Page for capture_generated_files
# ──────────────────────────────────────────────────────────────────────


class _Element:
    """Mock of a Playwright Locator-backed element."""

    def __init__(self, *, attrs: dict[str, str] | None = None, text: str = "") -> None:
        self.attrs = attrs or {}
        self.text = text
        self.clicked = False

    async def get_attribute(self, name: str, timeout: float | None = None) -> str | None:
        return self.attrs.get(name)

    async def inner_text(self, timeout: float | None = None) -> str:
        return self.text


class _Locator:
    def __init__(self, elements: list[_Element] | None = None) -> None:
        self._elements = list(elements or [])

    async def count(self) -> int:
        return len(self._elements)

    def nth(self, index: int) -> _Element:
        return self._elements[index]

    def locator(self, selector: str) -> "_Locator":
        return self  # The new pipeline only chains on the message locator.

    async def evaluate(self, script: str) -> Any:
        # ``message.evaluate`` is only used by ``_collect_card_anchors``,
        # which the orchestrator uses for the latest-turn scan.  Tests
        # that need the full DOM path build their own ``_Element``
        # objects and call ``capture_generated_files`` via the simpler
        # orchestrator branches.
        return []


class _ContextRequest:
    def __init__(self, responses: dict[str, bytes] | None = None) -> None:
        self._responses = responses or {}

    async def get(self, url: str, timeout: float = 0):
        body = self._responses.get(url, b"%PDF-1.4\nfallback")

        async def _body() -> bytes:
            return body

        return SimpleNamespace(ok=True, status=200, body=_body)


class _Page:
    """In-memory mock of a Playwright ``Page`` used by the new pipeline."""

    def __init__(
        self,
        *,
        message: _Locator | None,
        fetch_responses: dict[str, bytes] | None = None,
        scan_urls: list[str] | None = None,
        latest_assistant_index: int = 0,
    ) -> None:
        self._message = message
        self._scan_urls = scan_urls or []
        self._latest_assistant_index = latest_assistant_index
        self.context = SimpleNamespace(request=_ContextRequest(fetch_responses))
        self.url = "https://chatgpt.com/c/test"

    def locator(self, selector: str) -> _Locator:
        if selector.startswith("section[data-testid^="):
            return _Locator([self._message] if self._message else [])
        return _Locator([])

    async def evaluate(self, script: str) -> Any:
        # The "latest assistant turn" script returns the index of the
        # last assistant turn.  The page-scan script returns the URL
        # list captured by the test fixture.
        if "data-turn" in script or "conversation-turn-" in script:
            return self._latest_assistant_index
        if "estuary/content" in script or "interpreter/download" in script:
            return list(self._scan_urls)
        return []


# ──────────────────────────────────────────────────────────────────────
# capture_generated_files — DOM-first flow
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_generated_files_no_attachments(tmp_path):
    page = _Page(message=None, latest_assistant_index=-1)
    out = await capture_generated_files(page, tmp_path, attachment_timeout_ms=200)
    assert out == []


@pytest.mark.asyncio
async def test_capture_generated_files_href_fallback(tmp_path):
    """P2 path: href is fetched via the browser context (no click event)."""
    href = "https://chatgpt.com/backend-api/files/content/abc"
    page = _Page(
        message=_Locator([]),
        fetch_responses={href: b"# rainfall report\n"},
    )
    attachment = GeneratedAttachment(
        key=href,
        filename="降雨报告.md",
        href=href,
        click_target=None,  # forces the orchestrator to skip P0
    )
    path = await _download_via_href(page, attachment, tmp_path)
    assert path is not None
    assert path.read_bytes() == b"# rainfall report\n"


@pytest.mark.asyncio
async def test_capture_generated_files_page_scan_fallback(tmp_path):
    """P3 path: full-page URL scan when latest turn has no candidates."""
    estuary = (
        "https://chatgpt.com/backend-api/estuary/content"
        "?id=file_abc&fn=%E6%B5%B7%E5%8F%A3%E4%BB%8A%E6%97%A5%E9%99%8D%E9%9B%A8%E6%8A%A5%E5%91%8A.md"
    )
    page = _Page(
        message=_Locator([]),
        scan_urls=[estuary],
        fetch_responses={estuary: b"# rainfall report\n"},
    )
    out = await capture_generated_files(page, tmp_path, attachment_timeout_ms=200)
    assert len(out) == 1
    assert out[0].name.endswith("海口今日降雨报告.md")
    assert out[0].read_bytes() == b"# rainfall report\n"


@pytest.mark.asyncio
async def test_capture_generated_files_respects_latest_turn_scope(tmp_path):
    """History with attachments + empty current turn → []."""
    page = _Page(message=None, latest_assistant_index=-1)
    before = set(tmp_path.iterdir())
    out = await capture_generated_files(page, tmp_path, attachment_timeout_ms=200)
    assert out == []
    # And no historical file should be touched.
    assert set(tmp_path.iterdir()) == before


# ──────────────────────────────────────────────────────────────────────
# NetworkFileCapture (fallback class)
# ──────────────────────────────────────────────────────────────────────


def test_network_capture_records_only_attachment_like_responses():
    listeners = {}
    page = SimpleNamespace(
        url="https://chatgpt.com/c/x",
        on=lambda event, handler: listeners.__setitem__(event, handler),
    )
    capture = NetworkFileCapture(page)  # type: ignore[arg-type]
    capture.start()
    # Mimic two responses — one matching the hint, one not.
    matching = SimpleNamespace(
        url="https://chatgpt.com/backend-api/estuary/content?id=1",
        headers={"content-type": "application/octet-stream"},
    )
    unrelated = SimpleNamespace(
        url="https://chatgpt.com/some/other/path",
        headers={"content-type": "application/octet-stream"},
    )
    capture._on_response(matching)  # type: ignore[arg-type]
    capture._on_response(unrelated)  # type: ignore[arg-type]
    assert len(capture._captured) == 1
    assert "estuary/content" in capture._captured[0]["url"]
    capture.stop()
    assert capture._active is False


@pytest.mark.asyncio
async def test_network_capture_collect_attachment_returns_none_when_empty(tmp_path):
    page = SimpleNamespace(on=lambda _event, _handler: None)
    capture = NetworkFileCapture(page)  # type: ignore[arg-type]
    capture.start()
    attachment = SimpleNamespace(href="https://x/y/foo.docx", filename="foo.docx")
    result = await capture.collect_attachment(page, attachment, tmp_path)  # type: ignore[arg-type]
    assert result is None
    capture.stop()
