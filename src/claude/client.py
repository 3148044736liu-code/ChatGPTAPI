"""
Claude client — core interaction logic for claude.ai.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.

Same interface as ChatGPTClient so the API layer is provider-agnostic.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.config import Config
from src.claude.selectors import ClaudeSelectors
from src.browser.human import human_type, human_click, thinking_pause, random_delay
from src.claude.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    is_incomplete_response_text,
)
from src.chatgpt.models import ChatResponse
from src.log import setup_logging
from src.provider_errors import AttachmentUploadError, ProviderTimeoutError

log = setup_logging("claude_client")


class ClaudeClient:
    """
    High-level client for interacting with the Claude web interface.

    Requires a Playwright Page that is already logged in and on claude.ai.
    Same interface as ChatGPTClient for provider-agnostic API usage.
    """

    def __init__(self, page: Page, use_clipboard: bool = True) -> None:
        self._page = page
        self._use_clipboard = use_clipboard

    @property
    def page(self) -> Page:
        return self._page

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(self, text: str, image_paths: list[str] | None = None, file_paths: list[str] | None = None) -> ChatResponse:
        """
        Send a message to Claude and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files.

        Returns ChatResponse with the assistant's reply and metadata.
        """
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info("Sending message (chars=%s, attachments=%s)", len(text), len(all_attachments))
        start_time = time.time()

        # 0. Count existing assistant messages so we know when a new one appears
        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        # 1. Brief pause (human would take a moment to start typing)
        await random_delay(250, 700)

        # 1.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments)

        # 2. Find the chat input
        input_selector = await self._find_selector(ClaudeSelectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message
        await human_type(self._page, input_selector, text)

        # Small pause after pasting
        await random_delay(150, 350)

        # 4. Send the message
        sent = await self._click_send()
        if not sent:
            # Fallback: try pressing Enter
            log.info("Send button not found, trying Enter key")
            await self._page.keyboard.press("Enter")

        # 5. Wait for response
        log.info("Waiting for Claude response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            raise ProviderTimeoutError("Claude response did not complete before the timeout")

        # Small buffer after completion to let DOM settle
        await asyncio.sleep(0.5)

        # 6. Extract text content (Claude doesn't generate images like DALL-E)
        response_text = await extract_last_response_via_copy(
            self._page,
            previous_turn_signature=pre_turn_signature,
            use_clipboard=self._use_clipboard,
        )

        # If we only captured a transient status, retry
        if is_incomplete_response_text(response_text):
            log.warning("Extracted text looks incomplete/transient; retrying for final answer")
            for attempt in range(1, 3):
                await asyncio.sleep(4)
                await wait_for_response_complete(
                    self._page,
                    timeout_ms=90000,
                    previous_turn_signature=pre_turn_signature,
                )
                retry_text = await extract_last_response_via_copy(
                    self._page,
                    previous_turn_signature=pre_turn_signature,
                    use_clipboard=self._use_clipboard,
                )

                if retry_text and not is_incomplete_response_text(retry_text):
                    response_text = retry_text
                    log.info(f"Recovered final response text on retry {attempt}")
                    break

                if retry_text:
                    response_text = retry_text
                log.warning(f"Retry {attempt} still incomplete/transient")

            if is_incomplete_response_text(response_text):
                raise ProviderTimeoutError(
                    "Claude response remained incomplete after the timeout",
                    partial_output=response_text or None,
                )

        elapsed_ms = int((time.time() - start_time) * 1000)
        thread_id = await self.resolve_current_thread_id()

        log.info("Response received (elapsed_ms=%s, chars=%s)", elapsed_ms, len(response_text))

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            response_time_ms=elapsed_ms,
            images=[],
            has_images=False,
        )

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation by navigating to /new."""
        log.info("Starting new chat...")
        url = Config.CLAUDE_URL.rstrip("/") + "/new"
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        # Wait for the chat input to be visible
        for selector in ClaudeSelectors.CHAT_INPUT:
            try:
                await self._page.wait_for_selector(selector, timeout=10000, state="visible")
                log.debug(f"Chat input ready: {selector}")
                break
            except Exception:
                continue

        await random_delay(300, 600)
        log.info("New chat started (navigated to /new)")

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation thread."""
        url = f"{Config.CLAUDE_URL.rstrip('/')}/chat/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        await random_delay(800, 1500)
        log.info(f"Thread {thread_id} loaded")

    async def get_current_thread_url(self) -> str:
        """Get the current page URL (contains thread ID if in a conversation)."""
        return self._page.url

    # ── Sidebar ─────────────────────────────────────────────────

    async def list_threads(self) -> list[dict]:
        """
        Scrape the sidebar for recent conversation threads.

        Returns a list of dicts: [{id, title, url}, ...]
        """
        threads = []
        for selector in ClaudeSelectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    # Claude uses /chat/{uuid}
                    match = re.search(r"/chat/([a-f0-9-]+)", href)
                    if match:
                        threads.append({
                            "id": match.group(1),
                            "title": title,
                            "url": f"{Config.CLAUDE_URL.rstrip('/')}{href}",
                        })
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    async def open_thread_by_title(self, title: str, expected_thread_id: str) -> bool:
        for selector in ClaudeSelectors.SIDEBAR_THREAD_LINKS:
            try:
                for element in await self._page.query_selector_all(selector):
                    text = (await element.inner_text()).strip()
                    href = await element.get_attribute("href") or ""
                    match = re.search(r"/chat/([a-f0-9-]+)", href)
                    if text == title and match and match.group(1) == expected_thread_id:
                        await element.click()
                        await self._page.wait_for_load_state("domcontentloaded")
                        await asyncio.sleep(0.8)
                        return self._extract_thread_id() == expected_thread_id
            except Exception as error:
                log.debug("Claude Recent-list lookup failed: %s", error)
        return False

    async def resolve_current_thread_id(self) -> str:
        """Resolve the active conversation ID after SPA navigation or send."""
        thread_id = self._extract_thread_id()
        if thread_id:
            return thread_id
        for _ in range(10):
            try:
                active = await self._page.query_selector(
                    "a[aria-current='page'][href^='/chat/'], "
                    "a[data-active='true'][href^='/chat/']"
                )
                if active:
                    match = re.search(r"/chat/([a-f0-9-]+)", await active.get_attribute("href") or "")
                    if match:
                        return match.group(1)
                threads = await self.list_threads()
                if threads:
                    return threads[0]["id"]
            except Exception as error:
                log.debug("Current conversation ID lookup failed: %s", error)
            await asyncio.sleep(0.5)
        return ""

    async def rename_current_conversation(self, title: str) -> bool:
        """Rename through Claude's visible conversation menu when available."""
        thread_id = await self.resolve_current_thread_id()
        try:
            link = await self._page.query_selector(f"a[href^='/chat/{thread_id}']")
            if not link:
                return False
            await link.hover()
            menu = self._page.locator("button[aria-label*='menu' i], button[data-testid*='menu']").last
            await menu.click(timeout=5_000)
            await self._page.get_by_text(re.compile(r"^(Rename|重命名)$", re.I)).last.click(timeout=5_000)
            editor = self._page.locator("[role='dialog'] input, input[aria-label*='title' i]").last
            await editor.fill(title)
            await editor.press("Enter")
            await asyncio.sleep(0.8)
            return any(item["id"] == thread_id and item["title"] == title for item in await self.list_threads())
        except Exception as error:
            log.error("Claude conversation rename failed: %s", error)
            return False

    # ── Private Helpers ─────────────────────────────────────────

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """Try each selector in the fallback list. Return the first one that matches."""
        for selector in selectors:
            try:
                el = await self._page.wait_for_selector(
                    selector,
                    timeout=Config.SELECTOR_TIMEOUT,
                    state="visible",
                )
                if el:
                    log.debug(f"Found {name} via: {selector}")
                    return selector
            except Exception:
                log.debug(f"Selector miss for {name}: {selector}")
                continue

        log.warning(f"No working selector found for: {name}")
        return None

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        selector = await self._find_selector(ClaudeSelectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.debug("Send button clicked")
            return True
        return False

    async def _upload_files(self, file_paths: list[str]) -> None:
        """Upload files to Claude's input area."""
        from pathlib import Path

        valid_paths = []
        missing_files: list[str] = []
        for p in file_paths:
            path = Path(p)
            if path.exists() and path.is_file():
                valid_paths.append(str(path.resolve()))
            else:
                missing_files.append(str(p))

        if missing_files:
            raise AttachmentUploadError(missing_files, "Attachment file is missing")
        if not valid_paths:
            raise AttachmentUploadError(list(file_paths), "No valid attachment files were provided")

        log.info(f"Uploading {len(valid_paths)} file(s)...")

        # Find the file input element
        file_input = None
        for selector in ClaudeSelectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
                if elements:
                    file_input = elements[0]
                    log.debug(f"Found file input: {selector}")
                    break
            except Exception:
                continue

        if file_input:
            await file_input.set_input_files(valid_paths)
            log.info(f"Set {len(valid_paths)} file(s) on file input")
        else:
            log.info("No file input found via selectors, trying broad input[type=file]")
            try:
                await self._page.set_input_files("input[type='file']", valid_paths)
                log.info(f"Set {len(valid_paths)} file(s) via broad selector")
            except Exception as e:
                log.error(f"Failed to upload files: {e}")
                raise RuntimeError(f"Could not upload files: {e}")

        failed = []
        for path in valid_paths:
            if not await self._wait_for_attachment(Path(path).name):
                failed.append(Path(path).name)
        if failed:
            raise AttachmentUploadError(failed)
        log.info("All %s attachment(s) are READY", len(valid_paths))

    async def _wait_for_attachment(self, filename: str, timeout_ms: int = 10_000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                state = await self._page.evaluate(
                    """
                    (name) => {
                        const alerts = [...document.querySelectorAll("[role='alert'], [class*='toast']")];
                        const failed = alerts.some(el => /failed|couldn't upload|too large/i.test(el.innerText || ''));
                        const visible = [...document.querySelectorAll('body *')].some(el => {
                            const rect = el.getBoundingClientRect();
                            if (!rect.width || !rect.height || rect.height > 300) return false;
                            const value = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`;
                            return value.toLowerCase().includes(name.toLowerCase());
                        });
                        return {failed, visible};
                    }
                    """,
                    filename,
                )
                if state.get("failed"):
                    return False
                if state.get("visible"):
                    return True
            except Exception as error:
                log.debug("Attachment readiness check failed: %s", error)
            await asyncio.sleep(0.4)
        return False

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        # Claude uses /chat/{uuid}
        match = re.search(r"/chat/([a-f0-9-]+)", url)
        return match.group(1) if match else ""
