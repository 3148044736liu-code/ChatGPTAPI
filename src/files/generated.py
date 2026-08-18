"""Capture downloadable files from the latest ChatGPT assistant turn."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from patchright.async_api import Page

from src.log import setup_logging

log = setup_logging("generated_files")

_FILE_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".json", ".md", ".pdf", ".ppt", ".pptx",
    ".rtf", ".txt", ".xls", ".xlsx", ".xml", ".zip",
}


def _safe_name(value: str, fallback: str) -> str:
    name = Path(unquote(value)).name.strip() or fallback
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180]


def _looks_downloadable(href: str, download_name: str | None) -> bool:
    if download_name:
        return True
    lowered = href.lower()
    if "sandbox:" in lowered or "/mnt/data/" in lowered:
        return True
    suffix = Path(urlparse(href).path).suffix.lower()
    return suffix in _FILE_EXTENSIONS


async def capture_generated_files(page: Page, destination: Path) -> list[Path]:
    """Best-effort download of file links in the latest assistant response."""
    destination.mkdir(parents=True, exist_ok=True)
    captured: list[Path] = []

    try:
        turns = page.locator("section[data-testid^='conversation-turn-']")
        turn_count = await turns.count()
        if turn_count == 0:
            return captured
        anchors = turns.nth(turn_count - 1).locator("a")
        anchor_count = min(await anchors.count(), 20)
    except Exception as error:
        log.debug(f"Could not locate response links: {error}")
        return captured

    for index in range(anchor_count):
        anchor = anchors.nth(index)
        try:
            href = await anchor.get_attribute("href") or ""
            download_name = await anchor.get_attribute("download")
            label = (await anchor.inner_text()).strip()
            if not href or not _looks_downloadable(href, download_name):
                continue

            fallback = f"generated_file_{index + 1}"
            parsed_name = Path(urlparse(href).path).name
            filename = _safe_name(download_name or parsed_name or label, fallback)
            target = destination / f"{uuid.uuid4().hex}_{filename}"

            if href.startswith(("http://", "https://")):
                response = await page.context.request.get(href, timeout=30_000)
                if response.ok:
                    target.write_bytes(await response.body())
                    captured.append(target)
                    continue

            try:
                async with page.expect_download(timeout=10_000) as download_info:
                    await anchor.click()
                download = await download_info.value
                suggested = _safe_name(download.suggested_filename, filename)
                target = destination / f"{uuid.uuid4().hex}_{suggested}"
                await download.save_as(str(target))
                captured.append(target)
            except Exception as error:
                log.debug(f"Link was not downloadable ({href[:100]}): {error}")
        except Exception as error:
            log.debug(f"Generated-file capture failed at anchor {index}: {error}")

    if captured:
        log.info(f"Captured {len(captured)} generated file(s)")
    return captured
