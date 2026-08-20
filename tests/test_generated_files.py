from __future__ import annotations

from pathlib import Path

import pytest

from src.api.session_service import (
    _generated_file_repair_prompt,
    _needs_generated_file_repair,
)
from src.files.generated import capture_generated_files, materialize_text_fallback


class _Collection:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _Candidate:
    def __init__(self, *, attrs=None, text="", card_text=""):
        self.attrs = attrs or {}
        self.text = text
        self.card_text = card_text
        self.clicked = False

    async def get_attribute(self, name, timeout=None):
        return self.attrs.get(name)

    async def inner_text(self, timeout=None):
        return self.text

    async def evaluate(self, _script):
        return self.card_text

    async def click(self):
        self.clicked = True


class _Turn:
    def __init__(self, candidates):
        self.candidates = candidates

    def locator(self, selector):
        if selector == "[data-message-author-role='assistant']":
            return _Collection([object()])
        return _Collection(self.candidates)


class _Response:
    ok = True
    headers = {}

    async def body(self):
        return b"# rainfall report\n"


class _Request:
    async def get(self, _url, timeout):
        assert timeout == 30_000
        return _Response()


class _Page:
    def __init__(self, turn, scan_urls=None):
        self.turn = turn
        self.context = type("Context", (), {"request": _Request()})()
        self.url = "https://chatgpt.com/c/test"
        self.scan_urls = scan_urls or []

    def locator(self, _selector):
        return _Collection([self.turn])

    async def evaluate(self, _script):
        return list(self.scan_urls)


@pytest.mark.asyncio
async def test_capture_generated_http_file_preserves_download_name(tmp_path):
    candidate = _Candidate(
        attrs={
            "href": "https://chatgpt.com/backend-api/files/content/abc",
            "download": "降雨报告.md",
        },
        text="下载",
    )
    captured = await capture_generated_files(_Page(_Turn([candidate])), tmp_path)

    assert [item.name for item in captured] == ["降雨报告.md"]
    assert captured[0].read_bytes() == b"# rainfall report\n"


@pytest.mark.asyncio
async def test_capture_relative_interpreter_download_uses_sandbox_filename(tmp_path):
    candidate = _Candidate(
        attrs={
            "href": (
                "/backend-api/conversation/test/interpreter/download"
                "?message_id=msg&sandbox_path=%2Fmnt%2Fdata%2Frainfall-report.md"
            )
        },
        text="Download",
    )

    captured = await capture_generated_files(_Page(_Turn([candidate])), tmp_path)

    assert [item.name for item in captured] == ["rainfall-report.md"]


@pytest.mark.asyncio
async def test_capture_falls_back_to_page_scan_when_no_candidates(tmp_path):
    # No clickable candidates in the turn, but the page exposes an estuary
    # attachment URL that should be picked up by the full-page scan fallback.
    estuary = (
        "https://chatgpt.com/backend-api/estuary/content"
        "?id=file_abc&fn=%E6%B5%B7%E5%8F%A3%E4%BB%8A%E6%97%A5%E9%99%8D%E9%9B%A8%E6%8A%A5%E5%91%8A.md"
        "&cd=attachment"
    )
    page = _Page(_Turn([]), scan_urls=[estuary])

    captured = await capture_generated_files(page, tmp_path)

    assert [item.name for item in captured] == ["海口今日降雨报告.md"]
    assert captured[0].read_bytes() == b"# rainfall report\n"


@pytest.mark.asyncio
async def test_capture_estuary_fn_query_param_used_as_filename(tmp_path):
    candidate = _Candidate(
        attrs={
            "href": (
                "https://chatgpt.com/backend-api/estuary/content"
                "?id=file_xyz&fn=%E6%8A%A5%E5%91%8A.docx&cd=attachment"
            )
        },
        text="下载",
    )

    captured = await capture_generated_files(_Page(_Turn([candidate])), tmp_path)

    assert [item.name for item in captured] == ["报告.docx"]


def test_file_reference_without_attachment_requests_one_repair_turn(tmp_path):
    response = "已经整理好了：下载降雨报告.md"

    assert _needs_generated_file_repair("请生成并下载一份降雨报告", response, [])
    assert not _needs_generated_file_repair(
        "请生成并下载一份降雨报告", response, [tmp_path / "降雨报告.md"]
    )
    prompt = _generated_file_repair_prompt("请生成降雨报告.md", response)
    assert "降雨报告.md" in prompt
    assert "真实文件" in prompt
    assert "不要只回复文件名" in prompt
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
