"""
Capture downloadable files from the latest ChatGPT assistant turn.

Architecture (priority order):
  P0  DOM attachment-card discovery scoped to the latest assistant turn
      → Playwright download event (expect_download + click)
  P1  NetworkFileCapture fallback (response.body for estuary/interpreter
      responses triggered by the click)
  P2  Direct href fetch via the browser context (preserves cookies/auth)
  P3  Full-page DOM URL scan (last resort)

Image responses keep their own channel (image_handler.py) and are excluded
here to keep the two flows independent.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from patchright.async_api import Page

from src.log import setup_logging

log = setup_logging("generated_files")

# Non-image attachment extensions the API will surface as a downloadable file.
_FILE_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".json", ".md", ".pdf", ".ppt", ".pptx",
    ".rtf", ".txt", ".xls", ".xlsx", ".xml", ".zip",
}
_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".heic",
}

# Tweakable timeouts (ms). Kept here so the values are easy to find.
_ATTACHMENT_RENDER_TIMEOUT_MS = 15_000
_DOM_POLL_INTERVAL_MS = 500
_DOM_STABLE_PASSES = 2
_DOWNLOAD_EVENT_TIMEOUT_MS = 30_000
_CLICK_TIMEOUT_MS = 8_000
_NETWORK_CAPTURE_WAIT_MS = 8_000
_BROWSER_FETCH_TIMEOUT_MS = 30_000
_MAX_FILE_BYTES = 50 * 1024 * 1024

# Network paths treated as auxiliary hints only — never used as a hard
# gate for "do we recognize a file".
_NETWORK_HINTS = (
    "estuary/content",
    "interpreter/download",
    "backend-api/files",
)

# Magic-byte signatures for the formats we most commonly care about.
_MAGIC_HEADERS: list[tuple[bytes, str]] = [
    (b"%PDF-", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"PK\x05\x06", ".zip"),
    (b"PK\x07\x08", ".zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".doc"),
    (b"{\r\n", ".json"),
    (b"{", ".json"),
    (b"[\r\n", ".json"),
    (b"[", ".json"),
]


# ──────────────────────────────────────────────────────────────────────
# Structured errors
# ──────────────────────────────────────────────────────────────────────


class GeneratedFileError(RuntimeError):
    """Base error for the generated-file pipeline."""

    code: str = "GENERATED_FILE_ERROR"

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


# Error code catalogue (used for both log lines and upstream retries).
NO_ATTACHMENT = "NO_ATTACHMENT"
ATTACHMENT_RENDER_TIMEOUT = "ATTACHMENT_RENDER_TIMEOUT"
DOWNLOAD_BUTTON_NOT_FOUND = "DOWNLOAD_BUTTON_NOT_FOUND"
DOWNLOAD_EVENT_TIMEOUT = "DOWNLOAD_EVENT_TIMEOUT"
NETWORK_CAPTURE_FAILED = "NETWORK_CAPTURE_FAILED"
BROWSER_FETCH_FAILED = "BROWSER_FETCH_FAILED"
FILE_EMPTY = "FILE_EMPTY"
FILE_INVALID = "FILE_INVALID"
FILE_REGISTER_FAILED = "FILE_REGISTER_FAILED"


# ──────────────────────────────────────────────────────────────────────
# Attachment model
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GeneratedAttachment:
    """A non-image attachment that the page exposes for the latest turn."""

    key: str  # stable key within the latest turn (href / sandbox path / label)
    filename: str  # best-effort display name from the card
    href: str | None = None
    card_text: str = ""
    aria_label: str = ""
    # Playwright Locator for the card root.  Optional so unit tests can
    # build plain dataclass instances without a live page.
    card_locator: Any = None
    # Locator for the click target that should fire the download.  May be
    # the card itself, an anchor, or a button inside the card.
    click_target: Any = None


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _safe_name(value: str, fallback: str) -> str:
    """Sanitize a candidate filename for Windows + length cap."""
    candidate = unquote(value or "").strip() or fallback
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate)
    # Collapse trailing dots / spaces which Windows rejects.
    candidate = candidate.rstrip(" .") or fallback
    return candidate[:180]


def _filename_from_href(href: str) -> str:
    """Extract a friendly filename from a ChatGPT file URL."""
    if not href:
        return ""
    try:
        parsed = urlparse(href)
    except Exception:
        return ""
    query = parse_qs(parsed.query or "")
    for key in ("filename", "file_name", "fn", "sandbox_path"):
        values = query.get(key) or []
        if values:
            value = values[0]
            if key == "sandbox_path" and value.startswith("/mnt/data/"):
                value = value[len("/mnt/data/") :]
            value = unquote(value).strip()
            if value:
                return Path(value).name
    return Path(parsed.path).name


def _looks_like_attachment(href: str | None, label: str | None) -> bool:
    """True when an href/label clearly points to a downloadable file."""
    text = (href or "") + " " + (label or "")
    lowered = text.lower()
    if "sandbox:" in lowered or "/mnt/data/" in lowered:
        return True
    # Determine the effective file suffix (path + filename query param).
    suffix = ""
    if href:
        try:
            parsed = urlparse(href)
        except Exception:
            parsed = None
        path_suffix = Path(parsed.path).suffix.lower() if parsed else ""
        query_suffix = ""
        if parsed is not None:
            for key in ("filename", "file_name", "fn"):
                value = (parse_qs(parsed.query or "").get(key) or [""])[0]
                if value:
                    query_suffix = Path(value).suffix.lower()
                    break
        # Prefer the more specific of the two.
        suffix = query_suffix or path_suffix
    # Image suffix short-circuits the whole check: those go through
    # image_handler, not the generated-file pipeline.
    if suffix in _IMAGE_EXTENSIONS:
        return False
    if href:
        href_low = href.lower()
        # ChatGPT renders "点击下载" cards as <a> with href pointing to
        # /backend-api/files/<id>/download?filename=... — recognize that.
        if "/files/" in href_low and (
            "/download" in href_low or suffix in _FILE_EXTENSIONS
        ):
            return True
    if suffix in _FILE_EXTENSIONS:
        return True
    # Last resort: any non-image extension.
    if suffix and suffix not in _IMAGE_EXTENSIONS and len(suffix) <= 6:
        return True
    # No href but the label itself names a file with a known extension
    # (e.g. "近一周热点事件总结_20260819.md Document").
    if not href and label:
        match = re.search(r"\.([a-z0-9]{1,5})\b", label.lower())
        if match and ("." + match.group(1)) in _FILE_EXTENSIONS:
            return True
    return False


def _detect_extension_from_magic(data: bytes, fallback: str) -> str:
    """Return an extension inferred from the leading bytes, if any."""
    for prefix, ext in _MAGIC_HEADERS:
        if data.startswith(prefix):
            return ext
    return fallback


def _looks_like_html_error(path: Path) -> bool:
    """Heuristic: did we save an HTML error page rather than the file?"""
    try:
        with path.open("rb") as handle:
            sample = handle.read(512)
    except OSError:
        return False
    head = sample.lstrip().lower()
    if not head:
        return False
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        # Look for telltale error phrases.
        for marker in (b"access denied", b"error", b"not found", b"forbidden"):
            if marker in head:
                return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Latest assistant turn
# ──────────────────────────────────────────────────────────────────────


async def get_latest_assistant_message(page: Page) -> Any | None:
    """Return a Playwright Locator scoped to the latest assistant turn.

    Returns ``None`` when no assistant turn exists yet.
    """
    try:
        count = await page.evaluate(
            """
            () => {
                const turns = Array.from(
                    document.querySelectorAll('section[data-testid^="conversation-turn-"]')
                );
                for (let idx = turns.length - 1; idx >= 0; idx--) {
                    const turn = turns[idx];
                    const role = turn.getAttribute('data-turn');
                    const hasAssistant = role === 'assistant' ||
                        Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                    if (hasAssistant) return idx;
                }
                return -1;
            }
            """
        )
    except Exception as error:
        log.debug(f"Could not enumerate turns: {error}")
        return None

    if count is None or int(count) < 0:
        return None

    try:
        turns = page.locator("section[data-testid^='conversation-turn-']")
        return turns.nth(int(count))
    except Exception as error:
        log.debug(f"Could not resolve latest assistant locator: {error}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Attachment discovery
# ──────────────────────────────────────────────────────────────────────


def _is_image_label(text: str) -> bool:
    lowered = (text or "").lower()
    if any(lowered.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    if "generated image" in lowered:
        return True
    return False


async def _collect_card_anchors(message) -> list[dict]:
    """Read DOM candidates from the latest assistant turn.

    Walks the turn root + every nested ShadowRoot so cards rendered inside
    ChatGPT's web-component tree are not missed.  Selectors are intentionally
    broad: the DOM shape of "点击下载" cards varies across ChatGPT builds and
    they may live in a ShadowRoot, be portal-mounted (offsetParent === null),
    or be plain ``<a>`` elements with no ``download`` attribute and an href
    that merely *contains* the word "download" or "/files/".
    """
    if message is None:
        return []
    script = """
    (root) => {
        if (!root) return [];
        const out = [];
        const seen = new Set();

        const FILE_RE = /\\.(md|pdf|csv|docx?|xlsx?|pptx?|json|zip|txt|rtf|xml)\\b/i;
        const DOWNLOAD_HINT = /下载|download|attachment|附件|文件/i;
        const HREF_ATTRS = ['href', 'data-href', 'data-url', 'data-file-url'];

        // Recursively walk a root, including any open ShadowRoot children.
        function* walk(node) {
            yield node;
            const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
            for (const el of all) {
                yield el;
                if (el.shadowRoot) {
                    for (const inner of walk(el.shadowRoot)) yield inner;
                }
            }
        }

        // Walk up the tree (including across ShadowRoot boundaries) to find
        // the nearest ancestor with a real file href.  ChatGPT's download
        // cards are usually a wrapper div with a nested <a href=".../download">;
        // the selector match may land on either layer, and the wrapper alone
        // has no href, so we have to look upward.
        function findHref(el) {
            let cur = el;
            let safety = 0;
            while (cur && safety++ < 20) {
                if (cur.getAttribute) {
                    for (let i = 0; i < HREF_ATTRS.length; i++) {
                        const v = cur.getAttribute(HREF_ATTRS[i]);
                        if (v) return v;
                    }
                }
                const parent = cur.parentElement
                    || (cur.parentNode && cur.parentNode.host)
                    || null;
                cur = parent;
            }
            return '';
        }

        // Find the actual element we should click: prefer the nearest <a> with
        // href, fall back to the matched element itself.
        function findClickTarget(el) {
            let cur = el;
            let safety = 0;
            while (cur && safety++ < 20) {
                if (cur.tagName === 'A' && cur.getAttribute('href')) return cur;
                const parent = cur.parentElement
                    || (cur.parentNode && cur.parentNode.host)
                    || null;
                cur = parent;
            }
            return el;
        }

        // Extract a usable filename from a card's text.  Order:
        //   1) anything between 《》 brackets, e.g. 点击下载《xxx.md》
        //   2) a bare filename-with-extension anywhere in the text
        //   3) the whole text trimmed
        function extractFilename(text) {
            if (!text) return '';
            const bracket = text.match(/《([^》]+)》/);
            if (bracket) return bracket[1].trim();
            const m = text.match(/[\\w.\\-_\\u4e00-\\u9fff]+\\.(md|pdf|docx?|xlsx?|pptx?|csv|json|zip|txt|rtf|xml)\\b/i);
            if (m) return m[0];
            return text.trim();
        }

        // Selector list ordered roughly by specificity.  Anything that *might*
        // be a clickable download card is considered; we filter by href/text
        // further down.
        const SELECTORS = [
            'a[href]',
            'button',
            '[role="button"]',
            '[role="link"]',
            '[data-testid*="download" i]',
            '[data-testid*="attachment" i]',
            '[class*="download" i]',
            '[class*="attachment" i]',
            '[class*="Download"]',
            '[data-href]',
            '[data-url]',
            '[data-file-url]',
        ];

        for (const el of walk(root)) {
            if (!el || el.nodeType !== 1) continue;

            // Skip elements that are explicitly hidden by CSS, but DO NOT use
            // offsetParent — ChatGPT portal-mounts its download cards onto
            // <body> so their offsetParent is often null even when visible.
            try {
                const style = el.ownerDocument && el.ownerDocument.defaultView
                    ? el.ownerDocument.defaultView.getComputedStyle(el)
                    : null;
                if (style && (style.display === 'none' || style.visibility === 'hidden')) {
                    continue;
                }
            } catch (e) { /* ignore */ }

            // Quick reject: not one of our interesting selectors.
            const matchedSelector = SELECTORS.some((sel) => {
                try { return el.matches(sel); } catch (e) { return false; }
            });
            if (!matchedSelector) continue;

            const href = findHref(el);
            const download = el.getAttribute && (el.getAttribute('download') || '');
            const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
            const title = (el.getAttribute && el.getAttribute('title')) || '';
            const cls = ((el.getAttribute && el.getAttribute('class')) || '').toString();
            const text = (el.innerText || el.textContent || '').trim();
            const filename = extractFilename(text);
            const blob = JSON.stringify({ href, text, aria, title, download, cls, filename });

            // Filter: at least one of these signals must be present.
            const hrefLooksFile = href && (
                /\\.(md|pdf|csv|docx?|xlsx?|pptx?|json|zip|txt|rtf|xml)\\b/i.test(href)
                || /\\/files\\//i.test(href)
                || /\\/download\\b/i.test(href)
                || /sandbox:/i.test(href)
                || /\\/mnt\\/data\\//i.test(href)
            );
            const textLooksFile = FILE_RE.test(text);
            const downloadAttr = !!download;
            const hint = DOWNLOAD_HINT.test(text + aria + title + cls);
            if (!href && !downloadAttr && !hint && !textLooksFile) continue;
            if (!href && !textLooksFile && !hint) continue;
            // Cheap image short-circuit: skip <a> wrapping a single generated image.
            if (/^https?:\\/\\/.+\\.(png|jpe?g|gif|webp|bmp|svg)\\b/i.test(href)
                && !textLooksFile && !hint && !downloadAttr) {
                continue;
            }

            // Dedup: prefer href as key (one file = one href); fall back to
            // a stable composite so text-only candidates don't get re-counted
            // when both the wrapper and an inner <span> are visited.
            const key = href || (filename ? `txt:${filename}` : `txt:${text}|${aria}|${cls}`);
            if (seen.has(key)) continue;
            seen.add(key);

            out.push({ href, download, aria, title, text, cls, filename, blob });
        }
        return out;
    }
    """
    try:
        return await message.evaluate(script) or []
    except Exception as error:
        log.debug(f"Could not enumerate attachment candidates: {error}")
        return []


async def find_attachments(message) -> list[GeneratedAttachment]:
    """Return non-image attachments declared in the latest assistant turn."""
    raw = await _collect_card_anchors(message)
    attachments: list[GeneratedAttachment] = []
    for item in raw:
        href = item.get("href") or ""
        download = item.get("download") or ""
        label = " ".join(
            [item.get("text") or "", item.get("aria") or "", item.get("title") or ""]
        ).strip()
        if _is_image_label(label):
            continue
        if not _looks_like_attachment(href or None, label or None):
            continue
        # Prefer the filename extracted by the JS extractor (handles
        # "点击下载《xxx.md》" → "xxx.md"); fall back to the old pipeline.
        js_filename = (item.get("filename") or "").strip()
        filename = _safe_name(
            js_filename
            or download
            or _filename_from_href(href)
            or label,
            f"generated_file_{len(attachments) + 1}",
        )
        attachments.append(
            GeneratedAttachment(
                key=href or f"{filename}|{label}",
                filename=filename,
                href=href or None,
                card_text=item.get("text") or "",
                aria_label=item.get("aria") or "",
            )
        )
    return attachments


async def wait_for_attachments(
    message,
    *,
    timeout_ms: int = _ATTACHMENT_RENDER_TIMEOUT_MS,
) -> list[GeneratedAttachment]:
    """Poll until the attachment list is stable for ``_DOM_STABLE_PASSES`` rounds."""
    deadline = time.monotonic() + timeout_ms / 1000
    previous: list[GeneratedAttachment] = []
    stable = 0
    while True:
        current = await find_attachments(message)
        # Compare by key so re-renders with the same links don't reset stability.
        if [a.key for a in current] == [a.key for a in previous]:
            stable += 1
        else:
            stable = 0
        previous = current
        if current and stable >= _DOM_STABLE_PASSES:
            return current
        if time.monotonic() >= deadline:
            if current:
                return current
            raise GeneratedFileError(
                ATTACHMENT_RENDER_TIMEOUT,
                "Attachments did not stabilise before the timeout",
            )
        await asyncio.sleep(_DOM_POLL_INTERVAL_MS / 1000)


# ──────────────────────────────────────────────────────────────────────
# Download strategies
# ──────────────────────────────────────────────────────────────────────


async def _materialize_download(
    download,
    attachment: GeneratedAttachment,
    destination: Path,
) -> Path:
    """Save a Playwright Download object to disk and return the path."""
    destination.mkdir(parents=True, exist_ok=True)
    suggested = _safe_name(download.suggested_filename or attachment.filename, attachment.filename)
    target = destination / f"{uuid.uuid4().hex}_{suggested}"
    try:
        await download.save_as(str(target))
    except Exception as error:
        raise GeneratedFileError(
            DOWNLOAD_EVENT_TIMEOUT,
            f"save_as failed for {attachment.filename}: {error}",
        ) from error
    return target


async def click_and_download_attachment(
    page: Page,
    attachment: GeneratedAttachment,
    destination: Path,
) -> Path:
    """P0: click the attachment and capture the Playwright download event."""
    if attachment.click_target is None:
        raise GeneratedFileError(
            DOWNLOAD_BUTTON_NOT_FOUND,
            f"No click target available for {attachment.filename}",
        )
    try:
        async with page.expect_download(timeout=_DOWNLOAD_EVENT_TIMEOUT_MS) as info:
            await attachment.click_target.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception as error:
        raise GeneratedFileError(
            DOWNLOAD_EVENT_TIMEOUT,
            f"Click did not produce a download event for {attachment.filename}: {error}",
        ) from error
    download = await info.value
    return await _materialize_download(download, attachment, destination)


async def _download_via_href(
    page: Page,
    attachment: GeneratedAttachment,
    destination: Path,
) -> Path | None:
    """P2: fetch the href with the browser context to preserve auth cookies."""
    href = attachment.href
    if not href or not href.startswith(("http://", "https://")):
        return None
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{uuid.uuid4().hex}_{attachment.filename}"
    try:
        response = await page.context.request.get(href, timeout=_BROWSER_FETCH_TIMEOUT_MS)
    except Exception as error:
        raise GeneratedFileError(
            BROWSER_FETCH_FAILED,
            f"Browser-context fetch failed for {href[:120]}: {error}",
        ) from error
    if not getattr(response, "ok", False):
        raise GeneratedFileError(
            BROWSER_FETCH_FAILED,
            f"Browser-context fetch returned status {response.status} for {href[:120]}",
        )
    body = await response.body()
    if not body:
        raise GeneratedFileError(FILE_EMPTY, f"Browser fetch returned empty body for {href[:120]}")
    target.write_bytes(body)
    return target


async def _browser_fetch_attachment_url(
    page: Page,
    href: str,
) -> bytes | None:
    """P3 helper: fetch bytes via the browser context (used by page-scan fallback)."""
    if not href or not href.startswith(("http://", "https://")):
        return None
    try:
        response = await page.context.request.get(href, timeout=_BROWSER_FETCH_TIMEOUT_MS)
    except Exception as error:
        log.debug(f"browser_fetch_attachment_url failed for {href[:120]}: {error}")
        return None
    if not getattr(response, "ok", False):
        return None
    return await response.body()


async def _scan_page_attachment_urls(page: Page) -> list[str]:
    """P4: enumerate candidate attachment URLs across the entire page.

    Broader than v1: pulls ``/backend-api/files/<id>/download`` URLs off any
    element with an href, ``data-href``, ``data-url``, or ``data-file-url``
    attribute — including those rendered inside ShadowRoots.  We also look
    for any element whose text mentions a downloadable extension and walk
    up to its nearest ancestor with an href (covers "点击下载《X.md》" cards
    where the link wraps a span, not the whole <a>).
    """
    try:
        return await page.evaluate(
            """
            () => {
                const out = new Set();
                const EXT_RE = /\\.(md|pdf|csv|docx?|xlsx?|pptx?|json|zip|txt|rtf|xml)\\b/i;
                const HREF_ATTRS = ['href', 'data-href', 'data-url', 'data-file-url'];

                const looksLikeFile = (href) => {
                    if (!href) return false;
                    const low = href.toLowerCase();
                    if (low.includes('estuary/content')
                        || low.includes('interpreter/download')
                        || (low.includes('/backend-api/files/') && low.includes('/download'))) {
                        return true;
                    }
                    return EXT_RE.test(low);
                };

                function* walk(node) {
                    yield node;
                    const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
                    for (const el of all) {
                        yield el;
                        if (el.shadowRoot) {
                            for (const inner of walk(el.shadowRoot)) yield inner;
                        }
                    }
                }

                for (const el of walk(document)) {
                    for (const attr of HREF_ATTRS) {
                        const raw = el.getAttribute && el.getAttribute(attr);
                        if (raw && looksLikeFile(raw)) {
                            try { out.add(new URL(raw, window.location.href).toString()); }
                            catch (e) { /* ignore */ }
                        }
                    }
                }
                return Array.from(out);
            }
            """
        ) or []
    except Exception as error:
        log.debug(f"Page attachment URL scan failed: {error}")
        return []


# ──────────────────────────────────────────────────────────────────────
# Validation & dedup
# ──────────────────────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_downloaded_file(path: Path) -> Path:
    """Raise GeneratedFileError on empty / oversized / HTML-error content."""
    if not path.is_file():
        raise GeneratedFileError(FILE_INVALID, f"File missing on disk: {path}")
    size = path.stat().st_size
    if size == 0:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise GeneratedFileError(FILE_EMPTY, f"Downloaded file is empty: {path}")
    if size > _MAX_FILE_BYTES:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise GeneratedFileError(
                FILE_INVALID,
                f"Downloaded file exceeds {_MAX_FILE_BYTES} bytes: {path}",
            )
    if _looks_like_html_error(path):
        try:
            path.unlink(missing_ok=True)
        finally:
            raise GeneratedFileError(
                FILE_INVALID,
                f"Downloaded content looks like an HTML error page: {path}",
            )
    return path


def deduplicate_files(paths: list[Path]) -> list[Path]:
    """Drop duplicates by SHA256 while preserving the first occurrence."""
    seen: set[str] = set()
    kept: list[Path] = []
    for path in paths:
        try:
            digest = _hash_file(path)
        except OSError as error:
            log.debug(f"SHA256 failed for {path}: {error}")
            continue
        if digest in seen:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        seen.add(digest)
        kept.append(path)
    return kept


# ──────────────────────────────────────────────────────────────────────
# Optional NetworkFileCapture (kept as fallback)
# ──────────────────────────────────────────────────────────────────────


class NetworkFileCapture:
    """Lightweight network-response capture used as a fallback only.

    The primary capture path is now DOM-based.  This class is retained
    so that if a click triggers a same-tab fetch (e.g. blob URL), the
    response body is still available.  Matching is intentionally loose
    — we capture any binary-looking response triggered after start().
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._active = False
        self._captured: list[dict] = []

    def start(self) -> None:
        if self._active:
            return
        self._page.on("response", self._on_response)
        self._active = True
        log.info("[GENERATED_FILE] NetworkFileCapture fallback started")

    def stop(self) -> None:
        # Playwright listeners cannot be removed cleanly; just stop accepting.
        self._active = False
        log.info(
            f"[GENERATED_FILE] NetworkFileCapture stopped ({len(self._captured)} response(s) seen)"
        )

    def _on_response(self, response) -> None:
        if not self._active:
            return
        url = response.url
        # First gate: URL hint (fast path) — same as before.
        if not any(hint in url for hint in _NETWORK_HINTS):
            return
        try:
            headers = dict(response.headers or {})
        except Exception:
            headers = {}
        content_type = (headers.get("content-type") or "").lower()
        disposition = (headers.get("content-disposition") or "").lower()
        # ChatGPT "/backend-api/files/<id>/download" responses may come back
        # as text/markdown or text/plain; we still want them.  Anything with
        # a "Content-Disposition: attachment" header is in scope regardless
        # of content-type.  We also still accept the binary/JSON signals
        # that the original code looked for.
        is_attachment = (
            "attachment" in disposition
            or content_type.startswith("application/")
            or "octet-stream" in content_type
            or content_type.startswith("text/markdown")
            or content_type.startswith("text/plain")
            or content_type.startswith("application/json")
        )
        if not is_attachment:
            return
        self._captured.append({"url": url, "headers": headers})

    def drain_for_attachment(self, attachment: GeneratedAttachment) -> list[dict]:
        """Return any captured responses that look related to ``attachment``."""
        key = attachment.href or attachment.filename
        related: list[dict] = []
        for entry in self._captured:
            url = entry.get("url", "")
            if key and (key in url or attachment.filename in url):
                related.append(entry)
        if not related and self._captured:
            # No URL-level correlation — return the most recent as best-effort.
            related = [self._captured[-1]]
        return related

    async def collect_attachment(
        self,
        page: Page,
        attachment: GeneratedAttachment,
        destination: Path,
    ) -> Path | None:
        """Try to materialize a captured response into a local file."""
        related = self.drain_for_attachment(attachment)
        if not related:
            return None
        for entry in related:
            url = entry.get("url", "")
            try:
                response = await page.context.request.get(url, timeout=_BROWSER_FETCH_TIMEOUT_MS)
            except Exception as error:
                log.debug(f"network_capture refetch failed for {url[:120]}: {error}")
                continue
            if not getattr(response, "ok", False):
                continue
            body = await response.body()
            if not body:
                continue
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{uuid.uuid4().hex}_{attachment.filename}"
            target.write_bytes(body)
            return target
        return None


# ──────────────────────────────────────────────────────────────────────
# Top-level entry point
# ──────────────────────────────────────────────────────────────────────


async def _collect_click_targets(message) -> dict[str, Any]:
    """Map each attachment key to a clickable Playwright element.

    Walks the same DOM the scanner walks and, for every selector match,
    resolves the *real* file href by walking up (across ShadowRoots) and
    pins the click target to the nearest ``<a href>`` ancestor — that is
    what the browser actually uses to start a download.  The dict key is
    the resolved href so callers can do ``targets[attachment.key]`` after
    the same walk has produced the attachment.
    """
    if message is None:
        return {}
    # Single JS pass: walk, walk-up for href, walk-up again for the <a>.
    # Returns pairs of (href, clickTargetElementHandle).
    try:
        pairs = await message.evaluate(
            """
            (root) => {
                if (!root) return [];
                const HREF_ATTRS = ['href', 'data-href', 'data-url', 'data-file-url'];
                const SELECTORS = [
                    'a[href]', 'button', '[role="button"]', '[role="link"]',
                    '[data-testid*="download" i]', '[data-testid*="attachment" i]',
                    '[class*="download" i]', '[class*="attachment" i]',
                    '[class*="Download"]', '[data-href]', '[data-url]', '[data-file-url]',
                ];
                function* walk(node) {
                    yield node;
                    const all = node.querySelectorAll ? node.querySelectorAll('*') : [];
                    for (const el of all) {
                        yield el;
                        if (el.shadowRoot) {
                            for (const inner of walk(el.shadowRoot)) yield inner;
                        }
                    }
                }
                function closestA(el) {
                    let cur = el, safety = 0;
                    while (cur && safety++ < 20) {
                        if (cur.tagName === 'A' && cur.getAttribute && cur.getAttribute('href')) return cur;
                        const p = cur.parentElement || (cur.parentNode && cur.parentNode.host) || null;
                        cur = p;
                    }
                    return null;
                }
                function closestHref(el) {
                    let cur = el, safety = 0;
                    while (cur && safety++ < 20) {
                        if (cur.getAttribute) {
                            for (let i = 0; i < HREF_ATTRS.length; i++) {
                                const v = cur.getAttribute(HREF_ATTRS[i]);
                                if (v) return v;
                            }
                        }
                        const p = cur.parentElement || (cur.parentNode && cur.parentNode.host) || null;
                        cur = p;
                    }
                    return '';
                }
                const out = [];
                const seen = new Set();
                for (const el of walk(root)) {
                    if (!el || el.nodeType !== 1) continue;
                    const matched = SELECTORS.some(s => { try { return el.matches(s); } catch(e) { return false; } });
                    if (!matched) continue;
                    const href = closestHref(el);
                    if (!href || seen.has(href)) continue;
                    seen.add(href);
                    const clickA = closestA(el) || el;
                    out.push({ href, clickA });
                }
                return out;
            }
            """
        )
    except Exception as error:
        log.debug(f"Click target walk failed: {error}")
        return {}

    targets: dict[str, Any] = {}
    for pair in pairs or []:
        href = pair.get("href") or ""
        click_handle = pair.get("clickA")
        if not href or not click_handle:
            continue
        try:
            element = click_handle.as_element() if hasattr(click_handle, "as_element") else None
            if element is not None:
                targets[href] = element
        except Exception:
            continue
    return targets


async def capture_generated_files(
    page: Page,
    destination: Path,
    *,
    network_capture: NetworkFileCapture | None = None,
    attachment_timeout_ms: int = _ATTACHMENT_RENDER_TIMEOUT_MS,
) -> list[Path]:
    """DOM-first capture of non-image attachments for the latest assistant turn."""
    log.info("[GENERATED_FILE] assistant response completed; scanning latest turn")

    message = await get_latest_assistant_message(page)
    if message is None:
        log.info("[GENERATED_FILE] NO_ATTACHMENT — no assistant turn located")
        return []

    try:
        attachments = await wait_for_attachments(message, timeout_ms=attachment_timeout_ms)
    except GeneratedFileError as error:
        log.warning(
            f"[GENERATED_FILE] {error.code} — attachments did not stabilise: {error}"
        )
        # Still try a full-page URL scan as a last resort.
        attachments = []

    # Re-resolve click targets so we can interact with each card.
    targets = await _collect_click_targets(message)
    for attachment in attachments:
        attachment.click_target = targets.get(attachment.key)
        # Tertiary fallback: if the walk found the href but missed the
        # click target (DOM re-rendered, ShadowRoot mount, key formatting
        # mismatch), locate the <a> by href directly.  This is the
        # practical fix for "ChatGPT rendered the download card but the
        # key we built does not match what the click scanner iterated
        # over".
        if attachment.click_target is None and attachment.href:
            try:
                escaped = attachment.href.replace('"', '\\"')
                direct = message.locator(f'a[href="{escaped}"]').first
                if await direct.count() > 0:
                    attachment.click_target = direct
            except Exception:
                pass
        # Final fallback: find a clickable element whose text/aria contains
        # the filename.
        if attachment.click_target is None and attachment.filename:
            try:
                fallback = message.get_by_text(
                    attachment.filename, exact=False
                ).first
                if await fallback.count() > 0:
                    attachment.click_target = fallback
            except Exception:
                pass

    if not attachments:
        log.info("[GENERATED_FILE] attachments found count=0 in latest turn")
        # P4 fallback: full-page URL scan
        urls = await _scan_page_attachment_urls(page)
        if not urls:
            return []
        destination.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for url in urls:
            data = await _browser_fetch_attachment_url(page, url)
            if not data:
                continue
            filename = _safe_name(_filename_from_href(url), Path(urlparse(url).path).name)
            target = destination / f"{uuid.uuid4().hex}_{filename}"
            target.write_bytes(data)
            saved.append(target)
        if not saved:
            return []
        return _safe_validate(saved)

    log.info(f"[GENERATED_FILE] attachments found count={len(attachments)}")
    destination.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for attachment in attachments:
        path = await _capture_one(page, attachment, destination, network_capture)
        if path is None:
            log.warning(
                f"[GENERATED_FILE] FAILED filename={attachment.filename} "
                "reason=no_download_event_no_network_response_no_valid_href"
            )
            continue
        saved.append(path)

    validated = _safe_validate(saved)
    deduped = deduplicate_files(validated)
    for path in deduped:
        size = path.stat().st_size
        digest = _hash_file(path)
        log.info(
            f"[GENERATED_FILE] saved path={path} size={size} sha256={digest}"
        )
    return deduped


def _safe_validate(paths: list[Path]) -> list[Path]:
    """Run validation but never raise; the caller wants the survivors."""
    out: list[Path] = []
    for path in paths:
        try:
            out.append(_validate_downloaded_file(path))
        except GeneratedFileError as error:
            log.warning(
                f"[GENERATED_FILE] validation failed path={path} code={error.code} reason={error}"
            )
    return out


async def _capture_one(
    page: Page,
    attachment: GeneratedAttachment,
    destination: Path,
    network_capture: NetworkFileCapture | None,
) -> Path | None:
    """Run the P0→P1→P2 cascade for a single attachment."""
    log.info(
        f"[GENERATED_FILE] attachment filename={attachment.filename} "
        f"href={(attachment.href or '')[:120]} has_download_button={attachment.click_target is not None}"
    )

    # P0: click + Playwright download event
    if attachment.click_target is not None:
        try:
            log.info(
                f"[GENERATED_FILE] clicking download filename={attachment.filename}"
            )
            path = await click_and_download_attachment(page, attachment, destination)
            try:
                _validate_downloaded_file(path)
            except GeneratedFileError as error:
                log.warning(
                    f"[GENERATED_FILE] browser download invalid filename={attachment.filename} "
                    f"code={error.code}"
                )
                path = None
            if path is not None:
                log.info(
                    f"[GENERATED_FILE] playwright download event fired filename={attachment.filename}"
                )
                return path
        except GeneratedFileError as error:
            log.warning(
                f"[GENERATED_FILE] browser download timeout filename={attachment.filename} "
                f"code={error.code}"
            )

    # P1: NetworkFileCapture fallback (response body for estuary/interpreter)
    if network_capture is not None:
        try:
            log.info(
                f"[GENERATED_FILE] checking network fallback filename={attachment.filename}"
            )
            path = await network_capture.collect_attachment(page, attachment, destination)
            if path is not None:
                try:
                    _validate_downloaded_file(path)
                except GeneratedFileError as error:
                    log.warning(
                        f"[GENERATED_FILE] network fallback invalid filename={attachment.filename} "
                        f"code={error.code}"
                    )
                    path = None
                if path is not None:
                    log.info(
                        f"[GENERATED_FILE] network fallback captured filename={attachment.filename}"
                    )
                    return path
        except Exception as error:
            log.debug(f"network_capture raised for {attachment.filename}: {error}")

    # P2: direct href fetch via browser context
    if attachment.href:
        try:
            log.info(
                f"[GENERATED_FILE] href browser fetch filename={attachment.filename}"
            )
            path = await _download_via_href(page, attachment, destination)
            if path is not None:
                try:
                    _validate_downloaded_file(path)
                except GeneratedFileError as error:
                    log.warning(
                        f"[GENERATED_FILE] href fetch invalid filename={attachment.filename} "
                        f"code={error.code}"
                    )
                    path = None
                if path is not None:
                    log.info(
                        f"[GENERATED_FILE] href fetch saved filename={attachment.filename}"
                    )
                    return path
        except GeneratedFileError as error:
            log.debug(
                f"[GENERATED_FILE] href fetch failed filename={attachment.filename} "
                f"code={error.code}"
            )

    return None


# ──────────────────────────────────────────────────────────────────────
# Repair / fallback materialization
# ──────────────────────────────────────────────────────────────────────
#
# These helpers handle the "ChatGPT talked about a file but did not
# actually attach one" cases:
#
#   - The response text mentions a filename, but no DOM card / download
#     event ever fires.  ``_needs_generated_file_repair`` tells the caller
#     to ask ChatGPT again; ``_generated_file_repair_prompt`` returns the
#     actual prompt to send.
#
#   - The response contains a fenced code block holding the file contents
#     (e.g. the model pasted the CSV rows inline).  ``materialize_text_fallback``
#     extracts the first block and writes it to disk so the caller can
#     register it via FileService like any other generated file.

_FILENAME_HINT = re.compile(
    r"([A-Za-z0-9_\-./\u4e00-\u9fff]+\."
    r"(?:csv|docx|doc|json|md|pdf|pptx|ppt|rtf|txt|xlsx|xls|xml|zip))",
    re.IGNORECASE,
)
_BINARY_FILE_EXTENSIONS = {
    ".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".zip",
}
_FENCED_CODE = re.compile(
    r"```([A-Za-z0-9_+-]*)\s*\n(.*?)\n```",
    re.DOTALL,
)


def _extract_filename_hint(text: str) -> str:
    """Pull the first plausible filename from ``text`` (Chinese supported)."""
    if not text:
        return ""
    match = _FILENAME_HINT.search(text)
    return Path(match.group(1)).name if match else ""


def _looks_like_file_request(message: str) -> bool:
    """True when the user's prompt clearly asks for a downloadable file."""
    if not message:
        return False
    lowered = message.lower()
    keywords = (
        "下载", "生成文件", "生成一份", "附件", "导出", "提供下载",
        "download", "attachment", "export", "save as", "create a file",
        "生成并下载", "做成文件", "给我一个文件", "输出一份文件",
    )
    if any(keyword in lowered for keyword in keywords):
        return True
    return bool(_extract_filename_hint(message))


def _needs_generated_file_repair(
    message: str,
    response: str,
    captured_paths: list[Path],
) -> bool:
    """True when the prompt asked for a file but nothing was downloaded."""
    if not _looks_like_file_request(message):
        return False
    if captured_paths:
        return False
    return bool(_extract_filename_hint(message) or _extract_filename_hint(response or ""))


def _generated_file_repair_prompt(message: str, response: str) -> str:
    """Build a follow-up prompt that asks ChatGPT to attach the real file."""
    filename = _extract_filename_hint(message) or _extract_filename_hint(response or "")
    target = filename or "the file"
    return (
        f"上一条回复里你提到了文件 {target}，但你并没有真正附加附件。\n"
        "请重新生成回复，**通过 ChatGPT 的文件附件功能**（不是仅仅"
        "在文本里写出文件名）把文件附在回复里。\n\n"
        "要求：\n"
        "1. 必须是一个**真实文件形式的可下载附件**，而不是文本里描述的文件名；\n"
        "2. 不要只回复文件名或文件说明文字；\n"
        "3. 如果内容很短，可以直接以该扩展名作为附件生成；\n"
        "4. 不要把文件内容塞进 fenced code block 代替附件，fenced code "
        "block 不是用户期望的下载文件。\n"
    )


def materialize_text_fallback(
    message: str,
    response: str,
    destination: Path,
) -> Path | None:
    """Extract a fenced code block from ``response`` and write it to disk.

    Returns ``None`` when the file would be a binary format (we cannot
    safely synthesize an .xlsx / .pdf from a code block).
    """
    if not response or not message:
        return None
    filename = _extract_filename_hint(message) or _extract_filename_hint(response)
    if not filename:
        return None
    suffix = Path(filename).suffix.lower()
    if suffix in _BINARY_FILE_EXTENSIONS:
        return None
    match = _FENCED_CODE.search(response)
    if not match:
        return None
    body = match.group(2).rstrip("\r\n") + "\n"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / _safe_name(filename, filename)
    target.write_text(body, encoding="utf-8")
    return target
