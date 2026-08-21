"""
Image handler — detects, extracts, and downloads generated images.

When ChatGPT generates an image via DALL-E, the response contains:
- An <img> tag with the image URL (hosted on openai.com)
- A "Image created" text indicator
- An image title/alt text (description of what was generated)

This module:
1. Detects if the last assistant message contains generated images
2. Extracts image URLs and metadata
3. Downloads images to local disk
4. Returns ImageInfo objects with URLs and local paths
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page

from src.config import Config
from src.selectors import Selectors
from src.chatgpt.models import ImageInfo
from src.log import setup_logging

log = setup_logging("image_handler")

_IMAGE_MATERIALIZE_TIMEOUT_MS = 15_000
_IMAGE_POLL_INTERVAL_MS = 250


async def _generated_image_snapshot(page: Page) -> dict:
    """Inspect the latest assistant turn for image markers and usable URLs."""
    result = await page.evaluate(r"""
        () => {
            const turns = Array.from(
                document.querySelectorAll('section[data-testid^="conversation-turn-"]')
            );
            let lastTurn = null;
            for (let idx = turns.length - 1; idx >= 0; idx--) {
                const turn = turns[idx];
                const role = turn.getAttribute('data-turn');
                const isAssistant = role === 'assistant' || Boolean(
                    turn.querySelector('[data-message-author-role="assistant"]')
                );
                if (isAssistant) {
                    lastTurn = turn;
                    break;
                }
            }
            if (!lastTurn) return {hasMarker: false, images: []};

            // Imagegen web components can place the real image in a ShadowRoot.
            const roots = [lastTurn];
            for (let idx = 0; idx < roots.length; idx++) {
                const root = roots[idx];
                const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const element of elements) {
                    if (element.shadowRoot) roots.push(element.shadowRoot);
                }
            }

            let title = '';
            for (const root of roots) {
                for (const button of root.querySelectorAll('button')) {
                    const text = (button.innerText || '').trim();
                    const bulletIdx = text.indexOf('•');
                    if (bulletIdx > -1) {
                        title = text.substring(bulletIdx + 1).trim();
                        break;
                    }
                }
                if (title) break;
            }
            if (!title) {
                for (const root of roots) {
                    for (const span of root.querySelectorAll('span.text-token-text-tertiary')) {
                        const text = (span.innerText || '').trim();
                        if (text.length > 5 && text.length < 200) {
                            title = text;
                            break;
                        }
                    }
                    if (title) break;
                }
            }

            let hasMarker = false;
            const found = new Map();
            const addImage = (url, alt = '') => {
                const normalized = (url || '').trim();
                if (!normalized || found.has(normalized)) return;
                found.set(normalized, {url: normalized, alt, title});
            };

            for (const root of roots) {
                const containers = root.querySelectorAll(
                    'div[id^="image-"], div[class*="imagegen-image"]'
                );
                if (containers.length > 0) hasMarker = true;

                const directImages = root.querySelectorAll(
                    'img[alt="Generated image"], '
                    + 'div[id^="image-"] img, '
                    + 'div[class*="imagegen-image"] img, '
                    + 'img[src*="backend-api/estuary"], '
                    + 'img[src*="backend-api/files"], '
                    + 'img[src^="blob:"]'
                );
                if (directImages.length > 0) hasMarker = true;
                for (const img of directImages) {
                    addImage(img.currentSrc || img.src || img.getAttribute('src'), img.alt || '');
                }

                // Keep the old large-image fallback for provider UI variants.
                for (const img of root.querySelectorAll('img')) {
                    const width = img.naturalWidth || img.width || 0;
                    const url = img.currentSrc || img.src || img.getAttribute('src') || '';
                    if (width > 200 && (
                        url.includes('backend-api/estuary')
                        || url.includes('backend-api/files')
                        || url.startsWith('blob:')
                    )) {
                        hasMarker = true;
                        addImage(url, img.alt || '');
                    }
                }

                // Some imagegen builds render the result as a CSS background.
                for (const container of containers) {
                    const candidates = [container, ...container.querySelectorAll('*')];
                    for (const element of candidates) {
                        const background = getComputedStyle(element).backgroundImage || '';
                        for (const match of background.matchAll(/url\((['"]?)(.*?)\1\)/g)) {
                            const url = match[2] || '';
                            if (
                                url.includes('backend-api/estuary')
                                || url.includes('backend-api/files')
                                || url.startsWith('blob:')
                            ) {
                                addImage(url, element.getAttribute('aria-label') || '');
                            }
                        }
                    }
                }
            }

            return {hasMarker, images: Array.from(found.values())};
        }
    """)
    return result if isinstance(result, dict) else {"hasMarker": False, "images": []}


async def detect_images_in_response(
    page: Page,
    *,
    timeout_ms: int = _IMAGE_MATERIALIZE_TIMEOUT_MS,
    poll_interval_ms: int = _IMAGE_POLL_INTERVAL_MS,
) -> list[dict]:
    """
    Check the last conversation turn for generated images.

    ChatGPT DALL-E image responses do NOT use data-message-author-role.
    Instead, images appear inside an article turn with:
    - img[alt="Generated image"]
    - div[id^="image-"] containers
    - src from chatgpt.com/backend-api/estuary/content

    Returns a list of dicts: [{url, alt, title}, ...] or empty list.
    """
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    marker_seen = False

    while True:
        snapshot = await _generated_image_snapshot(page)
        result = snapshot.get("images") or []
        if result:
            log.info(f"Detected {len(result)} generated image(s) in response")
            for i, img in enumerate(result):
                log.debug(
                    f"  Image {i+1}: alt='{img.get('alt', '')[:50]}', "
                    f"url={img.get('url', '')[:80]}..."
                )
            return result

        has_marker = bool(snapshot.get("hasMarker"))
        marker_seen = marker_seen or has_marker
        if not marker_seen:
            log.debug("No generated images detected in response")
            return []

        if time.monotonic() >= deadline:
            log.warning(
                "Generated image container detected, but no usable image URL "
                "materialized before timeout"
            )
            return []

        await asyncio.sleep(max(poll_interval_ms, 0) / 1000)


async def download_image(page: Page, url: str, filename_hint: str = "") -> str:
    """
    Download an image from a URL using the browser's fetch API.

    Uses the browser context so cookies/auth are preserved (required
    for OpenAI-hosted images that may need authentication).

    Returns the local file path.
    """
    Config.ensure_dirs()

    # Generate a filename from the URL or hint
    if filename_hint:
        # Clean the hint for use as filename
        safe_name = re.sub(r'[^\w\s-]', '', filename_hint)[:60].strip()
        safe_name = re.sub(r'\s+', '_', safe_name)
    else:
        # Use hash of URL as filename
        safe_name = hashlib.md5(url.encode()).hexdigest()[:12]

    # Add timestamp to avoid collisions
    ts = int(time.time())
    unique = uuid.uuid4().hex
    filename = f"{safe_name}_{ts}_{unique}.png"
    local_path = Config.IMAGES_DIR / filename

    log.info(f"Downloading image to {local_path}...")

    try:
        # Use browser's fetch to download (preserves auth cookies)
        image_data = await page.evaluate("""
            async (url) => {
                try {
                    const response = await fetch(url);
                    if (!response.ok) return null;
                    const blob = await response.blob();
                    const reader = new FileReader();
                    return new Promise((resolve) => {
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch (e) {
                    return null;
                }
            }
        """, url)

        if image_data and image_data.startswith("data:"):
            # Strip the data URL prefix to get raw base64
            import base64
            header, b64data = image_data.split(",", 1)

            # Detect actual format from MIME type
            if "png" in header:
                ext = ".png"
            elif "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
            else:
                ext = ".png"

            # Update filename with correct extension
            filename = f"{safe_name}_{ts}_{unique}{ext}"
            local_path = Config.IMAGES_DIR / filename

            raw_bytes = base64.b64decode(b64data)
            local_path.write_bytes(raw_bytes)

            size_kb = len(raw_bytes) / 1024
            log.info(f"Image saved: {local_path} ({size_kb:.1f} KB)")
            return str(local_path)

        else:
            log.warning("Failed to fetch image data via browser")

    except Exception as e:
        log.error(f"Image download failed: {e}", exc_info=True)

    # Do not fall back to a server-side URL fetch here.  The URL originated in
    # provider-controlled page content and must stay inside the authenticated
    # browser context.
    return ""


async def extract_images_from_response(page: Page) -> list[ImageInfo]:
    """
    Full pipeline: detect images in the last response, download them,
    and return ImageInfo objects with both URLs and local paths.
    """
    raw_images = await detect_images_in_response(page)

    if not raw_images:
        return []

    image_infos = []
    for img_data in raw_images:
        url = img_data.get("url", "")
        alt = img_data.get("alt", "")
        title = img_data.get("title", "")

        # Download the image
        hint = alt or title or "chatgpt_image"
        local_path = await download_image(page, url, filename_hint=hint)

        image_infos.append(ImageInfo(
            url=url,
            alt=alt,
            local_path=local_path,
            prompt_title=title,
        ))

    log.info(f"Processed {len(image_infos)} image(s)")
    return image_infos
