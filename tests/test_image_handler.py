from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.chatgpt import image_handler


class SnapshotPage:
    def __init__(self, snapshots: list[dict]) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    async def evaluate(self, script: str):
        assert "shadowRoot" in script
        assert "data-turn" in script
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


@pytest.mark.asyncio
async def test_text_response_returns_without_polling():
    page = SnapshotPage([{"hasMarker": False, "images": []}])

    result = await image_handler.detect_images_in_response(page)  # type: ignore[arg-type]

    assert result == []
    assert page.calls == 1


@pytest.mark.asyncio
async def test_image_container_waits_for_materialized_url():
    expected = {
        "url": "https://chatgpt.com/backend-api/estuary/content?id=image",
        "alt": "Generated image",
        "title": "Haikou weather",
    }
    page = SnapshotPage(
        [
            {"hasMarker": True, "images": []},
            {"hasMarker": True, "images": [expected]},
        ]
    )

    result = await image_handler.detect_images_in_response(
        page,  # type: ignore[arg-type]
        timeout_ms=1_000,
        poll_interval_ms=0,
    )

    assert result == [expected]
    assert page.calls == 2


@pytest.mark.asyncio
async def test_image_container_timeout_is_bounded():
    page = SnapshotPage([{"hasMarker": True, "images": []}])

    result = await image_handler.detect_images_in_response(
        page,  # type: ignore[arg-type]
        timeout_ms=0,
        poll_interval_ms=0,
    )

    assert result == []
    assert page.calls == 1


@pytest.mark.asyncio
async def test_extract_images_downloads_after_delayed_materialization(monkeypatch):
    expected = {
        "url": "blob:https://chatgpt.com/generated-image",
        "alt": "Generated image",
        "title": "Haikou weather",
    }
    page = SnapshotPage(
        [
            {"hasMarker": True, "images": []},
            {"hasMarker": True, "images": [expected]},
        ]
    )
    download = AsyncMock(return_value=r"C:\temp\haikou.png")
    monkeypatch.setattr(image_handler, "download_image", download)
    monkeypatch.setattr(image_handler, "_IMAGE_POLL_INTERVAL_MS", 0)

    result = await image_handler.extract_images_from_response(page)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].local_path == r"C:\temp\haikou.png"
    assert result[0].prompt_title == "Haikou weather"
    download.assert_awaited_once_with(
        page,
        expected["url"],
        filename_hint="Generated image",
    )
