"""
ChatGPT client — core interaction logic.

Sends messages, waits for responses, manages conversations.
Handles selector fallbacks and integrates human-like behavior.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from patchright.async_api import Page

from src.config import Config
from src.selectors import Selectors
from src.browser.human import human_type, human_click, thinking_pause, random_delay
from src.chatgpt.detector import (
    wait_for_response_complete,
    extract_last_response_via_copy,
    count_assistant_messages,
    get_latest_assistant_turn_signature,
    is_incomplete_response_text,
)
from src.chatgpt.image_handler import extract_images_from_response
from src.chatgpt.models import ChatResponse
from src.log import setup_logging
from src.provider_errors import AttachmentUploadError, ProviderTimeoutError

log = setup_logging("chatgpt_client")

# Extensions treated as images when deciding which composer file input to use.
_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".heic",
}


class ConversationNotFoundError(RuntimeError):
    """Raised when an exact sidebar conversation cannot be located."""


def _accept_is_image_only(accept: str) -> bool:
    """True when an input's accept attribute allows images and nothing else."""
    if not accept:
        return False
    parts = [p.strip() for p in accept.split(",") if p.strip()]
    return bool(parts) and all(p.startswith("image") for p in parts)


class ChatGPTClient:
    """
    High-level client for interacting with the ChatGPT web interface.

    Requires a Playwright Page that is already logged in and on chatgpt.com.
    """

    def __init__(self, page: Page, use_clipboard: bool = True) -> None:
        self._page = page
        self._use_clipboard = use_clipboard
        self._setup_network_logging()

    def _setup_network_logging(self) -> None:
        """Monitor network requests, WebSockets, and JS errors for debugging."""
        # Only log important API calls at INFO; sentinel/ping/heartbeat at DEBUG
        _important_paths = ("/f/conversation", "/conversations?", "/stream_status")

        def on_request(request):
            url = request.url
            if "backend-api" in url:
                if any(p in url for p in _important_paths):
                    log.info(f"NET REQ: {request.method} {url[:200]}")
                else:
                    log.debug(f"NET REQ: {request.method} {url[:200]}")

        async def on_response(response):
            url = response.url
            if "backend-api" in url:
                if any(p in url for p in _important_paths):
                    log.info(f"NET RESP: {response.status} {url[:200]}")
                else:
                    log.debug(f"NET RESP: {response.status} {url[:200]}")

        def on_request_failed(request):
            url = request.url
            failure = request.failure or "unknown"
            if "chrome-extension" not in url and "favicon" not in url:
                # Patchright internal injection is expected to fail
                if "patchright" in url:
                    log.debug(f"NET FAIL: {url[:150]} — {failure}")
                else:
                    log.warning(f"NET FAIL: {url[:150]} — {failure}")

        def on_console(msg):
            if msg.type == "error":
                log.info(f"JS ERROR: {msg.text[:300]}")
            elif msg.type == "warning":
                log.debug(f"JS WARNING: {msg.text[:300]}")

        def on_page_error(error):
            log.error(f"JS PAGE ERROR: {error}")

        def on_websocket(ws):
            log.debug(f"WS OPEN: {ws.url[:200]}")
            ws.on("framereceived", lambda payload: log.debug(f"WS RECV: {str(payload)[:200]}"))
            ws.on("framesent", lambda payload: log.debug(f"WS SEND: {str(payload)[:200]}"))
            ws.on("close", lambda _: log.debug(f"WS CLOSE: {ws.url[:200]}"))

        self._page.on("request", on_request)
        self._page.on("response", on_response)
        self._page.on("requestfailed", on_request_failed)
        self._page.on("console", on_console)
        self._page.on("pageerror", on_page_error)
        self._page.on("websocket", on_websocket)

    @property
    def page(self) -> Page:
        return self._page

    # ── Core: Send & Receive ────────────────────────────────────

    async def send_message(self, text: str, image_paths: list[str] | None = None, file_paths: list[str] | None = None) -> ChatResponse:
        """
        Send a message to ChatGPT and wait for the complete response.

        Args:
            text: The message text to send.
            image_paths: Optional list of local file paths to images to attach.
            file_paths: Optional list of local file paths to non-image files (PDF, etc.).

        Steps:
        1. Simulate thinking pause
        2. Upload images if provided
        3. Find and focus chat input
        4. Type message with human-like delays
        5. Click send
        6. Wait for response to complete
        7. Extract and return the response

        Returns ChatResponse with the assistant's reply and metadata.
        """
        all_attachments = (image_paths or []) + (file_paths or [])
        log.info("Sending message (chars=%s, attachments=%s)", len(text), len(all_attachments))
        start_time = time.time()

        # 0. Check page health — recover from DNS errors before trying to send
        page_error = await self._detect_page_error()
        if page_error:
            log.warning(f"Page error detected before send: {page_error}")
            raise RuntimeError(f"Page is in error state: {page_error}")

        # 0.5 Count existing assistant messages so we know when a new one appears
        pre_count = await count_assistant_messages(self._page)
        pre_turn_signature = await get_latest_assistant_turn_signature(self._page)
        log.debug(f"Assistant messages before send: {pre_count}")
        log.debug(f"Latest assistant turn before send: {pre_turn_signature}")

        # 0.5 Check for and dismiss any blocking dialogs/overlays
        await self._dismiss_overlays()

        # 1. Brief pause (human would take a moment to start typing)
        await random_delay(100, 300)

        # 1.5. Upload files/images if provided
        if all_attachments:
            await self._upload_files(all_attachments)

        # 2. Find the chat input (retry once after dismissing overlays if not found)
        input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            # An overlay may have blocked it — dismiss and retry
            log.info("Chat input not found on first try, dismissing overlays and retrying...")
            await self._dismiss_overlays()
            await asyncio.sleep(1)
            input_selector = await self._find_selector(Selectors.CHAT_INPUT, "chat input")
        if not input_selector:
            raise RuntimeError("Could not find chat input element")

        # 3. Paste the message (all at once)
        await human_type(self._page, input_selector, text)

        # 3.5 Verify the composer received the text; retry insertion when it
        #     is empty (typing right after staging an attachment can drop).
        for retry in range(2):
            if await self._composer_has_text():
                break
            log.info(f"Composer empty after typing — retrying insertion ({retry + 1}/2)")
            await asyncio.sleep(1.2)
            await human_type(self._page, input_selector, text)

        # 4. Poll briefly for auto-submit (execCommand can trigger
        #    f/conversation automatically in the current frontend).
        #    If a new assistant turn appeared, skip the send button click.
        auto_submitted = False
        for _ in range(6):  # poll up to ~3s in 0.5s intervals
            await asyncio.sleep(0.5)
            post_count = await count_assistant_messages(self._page)
            if post_count > pre_count:
                auto_submitted = True
                break

        if auto_submitted:
            log.info("ChatGPT auto-submitted after text entry — skipping send button click")
        else:
            # Attachments can remain in a processing state after their chip is
            # visible. Wait for ChatGPT to enable the send button instead of
            # pressing Enter against a disabled composer and then waiting for
            # a response that was never submitted.
            ready_timeout = 20000 if all_attachments else 5000
            log.info("No auto-submit detected, waiting for send button readiness")
            sent = False
            if await self._wait_for_send_ready(ready_timeout):
                sent = await self._click_send()
            if not sent:
                # Button disabled or missing — the text may have been lost.
                if not await self._composer_has_text():
                    log.info("Composer empty at send time — re-inserting text and retrying")
                    await human_type(self._page, input_selector, text)
                    await asyncio.sleep(1.0)
                    if await self._wait_for_send_ready(5000):
                        sent = await self._click_send()
                if not sent:
                    if all_attachments:
                        raise RuntimeError(
                            "Attachment staged but chat send button remained disabled"
                        )
                    log.info("Send button not available, trying Enter key")
                    await self._page.keyboard.press("Enter")

        # 5. Wait for response with message count awareness
        log.info("Waiting for ChatGPT response...")
        expected_count = pre_count + 1
        completed = await wait_for_response_complete(
            self._page,
            expected_msg_count=expected_count,
            previous_turn_signature=pre_turn_signature,
        )

        if not completed:
            raise ProviderTimeoutError("ChatGPT response did not complete before the timeout")

        # Small buffer after completion to let DOM settle
        await asyncio.sleep(0.2)

        # 6. Check for generated images in the response FIRST
        #    (image turns have no copy button, so we must detect images
        #    before trying copy-button extraction)
        images = await extract_images_from_response(self._page)
        has_images = len(images) > 0

        # 7. Extract text content
        if has_images:
            # Image responses don't have a copy button — extract text
            # from the turn's DOM instead (will get the image title/desc)
            response_text = await self._extract_image_turn_text(pre_turn_signature)
            log.info(f"Response contains {len(images)} generated image(s)")
            for img in images:
                log.info(f"  Image: {img.alt or img.prompt_title} → {img.local_path}")
        else:
            # Standard text response — use copy button (most reliable)
            response_text = await extract_last_response_via_copy(
                self._page,
                previous_turn_signature=pre_turn_signature,
                use_clipboard=self._use_clipboard,
            )

            # If extraction returned empty, retry a few times (DOM may not be settled)
            if not response_text.strip():
                log.warning("Empty response extracted — retrying after short wait")
                for retry in range(1, 4):
                    await asyncio.sleep(1.5 * retry)
                    response_text = await extract_last_response_via_copy(
                        self._page,
                        previous_turn_signature=pre_turn_signature,
                        use_clipboard=self._use_clipboard,
                    )
                    if response_text.strip():
                        log.info(f"Got response on extraction retry {retry}")
                        break

            # If we only captured a transient status (e.g. "Pro thinking"),
            # keep waiting and retry extraction on the same new turn.
            if is_incomplete_response_text(response_text):
                log.warning("Extracted text looks incomplete/transient; retrying for final answer")
                for attempt in range(1, 3):
                    await asyncio.sleep(2)
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
                        "ChatGPT response remained incomplete after the timeout",
                        partial_output=response_text or None,
                    )

        elapsed_ms = int((time.time() - start_time) * 1000)
        thread_id = await self.resolve_current_thread_id()

        log.info(
            "Response received (elapsed_ms=%s, chars=%s, images=%s)",
            elapsed_ms, len(response_text), len(images),
        )

        return ChatResponse(
            message=response_text,
            thread_id=thread_id,
            thread_url=self._page.url if thread_id else "",
            response_time_ms=elapsed_ms,
            images=images,
            has_images=has_images,
        )

    # ── Navigation ──────────────────────────────────────────────

    async def new_chat(self) -> None:
        """Start a new conversation.

        Strategy order:
        1. SPA button click (avoids DNS issues, preserves browser state)
        2. JavaScript location change (no DNS lookup needed if page is loaded)
        3. Full page.goto() (last resort — may fail with DNS errors)
        """
        # Already on a fresh chat — nothing to do
        if "chatgpt.com" in self._page.url:
            try:
                turn_count = await self._page.evaluate(
                    "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                )
                if turn_count == 0:
                    log.info("Already on a fresh chat — skipping navigation")
                    return
            except Exception:
                pass

        # Strategy 1: SPA button click
        for selector in Selectors.NEW_CHAT_BUTTON:
            try:
                btn = await self._page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"New chat via SPA button: {selector}")
                    await asyncio.sleep(1)
                    # Verify we're on a fresh chat
                    try:
                        turn_count = await self._page.evaluate(
                            "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
                        )
                        if turn_count == 0:
                            await self._wait_for_chat_input()
                            return
                    except Exception:
                        pass
            except Exception:
                continue

        # Strategy 2: JavaScript navigation (avoids DNS lookup)
        try:
            log.info("New chat via JS navigation...")
            await self._page.evaluate("window.location.href = '/'")
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            page_error = await self._detect_page_error()
            if not page_error:
                log.info("New chat started (JS navigation)")
                await self._wait_for_chat_input()
                return
        except Exception as e:
            log.warning(f"JS navigation failed: {e}")

        # Strategy 3: Full page.goto() — last resort
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            log.info(f"New chat via page.goto (attempt {attempt}/{max_attempts})...")
            try:
                await self._page.goto(
                    Config.CHATGPT_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                log.warning(f"page.goto failed (attempt {attempt}): {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise

            page_error = await self._detect_page_error()
            if page_error:
                log.error(f"Page error after goto (attempt {attempt}): {page_error}")
                if attempt < max_attempts:
                    await asyncio.sleep(attempt * 3)
                    continue
                raise RuntimeError(f"Page error persists after {max_attempts} attempts: {page_error}")

            log.info("New chat started (page.goto)")
            await self._wait_for_chat_input()
            return

    async def _wait_for_chat_input(self) -> None:
        """Wait for the chat input to become visible and interactive."""
        for selector in Selectors.CHAT_INPUT:
            try:
                await self._page.wait_for_selector(selector, timeout=10000, state="visible")
                log.debug(f"Chat input ready: {selector}")
                # Brief settle for React handlers to attach
                await asyncio.sleep(0.5)
                return
            except Exception:
                continue
        log.warning("Chat input not found — page may not be fully ready")

    async def wait_chatgpt_ready(self, timeout: int = 30_000) -> None:
        """Wait for a healthy, logged-in ChatGPT composer instead of sleeping."""
        deadline = time.monotonic() + timeout / 1000
        last_error = "composer not ready"
        while time.monotonic() < deadline:
            if self._page.is_closed():
                raise RuntimeError("ChatGPT page is closed")
            page_error = await self._detect_page_error()
            if page_error:
                last_error = page_error
            else:
                for selector in Selectors.CHAT_INPUT:
                    try:
                        composer = self._page.locator(selector).first
                        if await composer.is_visible():
                            return
                    except Exception:
                        continue
            await asyncio.sleep(0.25)
        raise RuntimeError(f"ChatGPT page did not become ready: {last_error}")

    async def recover_page(
        self,
        conversation_url: str | None = None,
        expected_thread_id: str | None = None,
    ) -> None:
        """Recover page health and restore the requested conversation."""
        if self._page.is_closed():
            raise RuntimeError("Cannot recover a closed ChatGPT page")
        errors: list[str] = []
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=30_000)
            await self.wait_chatgpt_ready()
            if conversation_url and not await self.verify_current_conversation(expected_thread_id or ""):
                await self._page.goto(conversation_url, wait_until="domcontentloaded", timeout=30_000)
                await self.wait_chatgpt_ready()
            if expected_thread_id and not await self.verify_current_conversation(expected_thread_id):
                raise RuntimeError("Reload restored the wrong conversation")
            return
        except Exception as error:
            errors.append(str(error))

        try:
            await self._page.goto(Config.CHATGPT_URL, wait_until="domcontentloaded", timeout=30_000)
            await self.wait_chatgpt_ready()
            if conversation_url:
                await self._page.goto(conversation_url, wait_until="domcontentloaded", timeout=30_000)
                await self.wait_chatgpt_ready()
            if expected_thread_id and not await self.verify_current_conversation(expected_thread_id):
                raise RuntimeError("Home recovery restored the wrong conversation")
            return
        except Exception as error:
            errors.append(str(error))
        raise RuntimeError("ChatGPT page recovery failed: " + " | ".join(errors))

    async def _detect_page_error(self) -> str | None:
        """Check if the current page shows a browser or ChatGPT error."""
        try:
            return await self._page.evaluate(
                """
                () => {
                    const body = document.body ? document.body.innerText : '';
                    const title = document.title || '';
                    if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'DNS_PROBE_FINISHED_NXDOMAIN';
                    if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'ERR_NAME_NOT_RESOLVED';
                    if (body.includes('ERR_CONNECTION_REFUSED')) return 'ERR_CONNECTION_REFUSED';
                    if (body.includes('ERR_INTERNET_DISCONNECTED')) return 'ERR_INTERNET_DISCONNECTED';
                    if (body.includes('ERR_CONNECTION_TIMED_OUT')) return 'ERR_CONNECTION_TIMED_OUT';
                    if (title.includes("can't be reached") || title.includes("is not available"))
                        return 'page_unreachable';
                    if (/captcha|verify you are human|unusual activity/i.test(body)) return 'CAPTCHA challenge';
                    const composer = document.querySelector(
                        '#prompt-textarea, textarea[name="prompt-textarea"], textarea[aria-label="Chat with ChatGPT"], div[contenteditable="true"]'
                    );
                    if (/log in|sign in/i.test(body) && !composer) return 'login required';
                    if (/rate limit|too many requests/i.test(body)) return 'rate limit';
                    if (/high demand|try again later/i.test(body)) return 'high demand';
                    if (body.includes('Something went wrong')) return 'ChatGPT_error';
                    return null;
                }
                """
            )
        except Exception:
            return None

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Navigate to an existing conversation thread."""
        url = f"{Config.CHATGPT_URL}/c/{thread_id}"
        log.info(f"Navigating to thread: {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await self.wait_chatgpt_ready()
        if not await self.verify_current_conversation(thread_id):
            raise RuntimeError(f"Thread navigation verification failed: {thread_id}")
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
        for selector in Selectors.SIDEBAR_THREAD_LINKS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    title = (await el.inner_text()).strip()
                    match = re.search(r"/c/([A-Za-z0-9-]+)", href)
                    if match:
                        threads.append({
                            "id": match.group(1),
                            "title": title,
                            "url": f"{Config.CHATGPT_URL}{href}",
                        })
                if threads:
                    break
            except Exception as e:
                log.debug(f"Sidebar scrape with {selector} failed: {e}")

        log.info(f"Found {len(threads)} threads in sidebar")
        return threads

    async def find_conversation_row(
        self,
        conversation_title: str | None = None,
        *,
        conversation_id: str | None = None,
        allow_active: bool = False,
        timeout: int = 10_000,
    ):
        """Locate one exact sidebar row, scrolling Recent history as needed."""
        if not conversation_title and not conversation_id:
            raise ValueError("conversation_title or conversation_id is required")
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            for selector in Selectors.SIDEBAR_THREAD_LINKS:
                links = self._page.locator(selector)
                try:
                    count = await links.count()
                except Exception:
                    continue
                for index in range(count):
                    link = links.nth(index)
                    try:
                        href = await link.get_attribute("href") or ""
                        text = (await link.inner_text()).strip()
                    except Exception:
                        continue
                    match = re.search(r"/c/([A-Za-z0-9-]+)", href)
                    if conversation_title and text != conversation_title:
                        continue
                    if conversation_id and (not match or match.group(1) != conversation_id):
                        active = await link.get_attribute("data-active")
                        if not allow_active or active is None:
                            continue
                    row = link.locator(
                        "xpath=ancestor::*[(self::li or self::div) and .//button][1]"
                    )
                    if await row.count():
                        return row
                    return link

            moved = await self._page.evaluate(
                """() => {
                    const nav = document.querySelector('nav');
                    if (!nav) return false;
                    const candidates = [nav, ...nav.querySelectorAll('*')];
                    const scroller = candidates.find(el => el.scrollHeight > el.clientHeight + 20);
                    if (!scroller) return false;
                    const before = scroller.scrollTop;
                    scroller.scrollTop += Math.max(300, scroller.clientHeight * 0.8);
                    return scroller.scrollTop > before;
                }"""
            )
            if not moved:
                break
            await asyncio.sleep(0.25)
        label = conversation_title or conversation_id
        raise ConversationNotFoundError(f"Conversation not found in Recent history: {label}")

    async def _conversation_link(self, row, conversation_id: str | None = None):
        href = await row.get_attribute("href")
        if href:
            return row
        selector = "a[href^='/c/']"
        if conversation_id:
            selector = f"a[href^='/c/{conversation_id}']"
        return row.locator(selector).first

    async def open_thread_by_title(self, title: str, expected_thread_id: str) -> bool:
        """Open an exact Recent-sidebar row and verify its conversation ID."""
        try:
            row = await self.find_conversation_row(
                title, conversation_id=expected_thread_id, timeout=10_000
            )
            link = await self._conversation_link(row, expected_thread_id)
            await link.click()
            await self.wait_chatgpt_ready()
            return await self.verify_current_conversation(expected_thread_id)
        except Exception as error:
            log.debug("Recent-list conversation open failed: %s", error)
            return False

    async def resolve_current_thread_id(self) -> str:
        """Resolve the active conversation ID after SPA navigation or send."""
        thread_id = self._extract_thread_id()
        if thread_id:
            return thread_id
        for _ in range(10):
            try:
                active = await self._page.query_selector(
                    "nav a[aria-current='page'][href^='/c/'], "
                    "nav a[data-active='true'][href^='/c/']"
                )
                if active:
                        match = re.search(r"/c/([A-Za-z0-9-]+)", await active.get_attribute("href") or "")
                        if match:
                            return match.group(1)
            except Exception as error:
                log.debug("Current conversation ID lookup failed: %s", error)
            await asyncio.sleep(0.5)
        return ""

    async def verify_current_conversation(self, expected_thread_id: str) -> bool:
        """Verify the active URL/ARIA state identifies the expected thread."""
        if not expected_thread_id:
            return False
        for _ in range(20):
            if self._extract_thread_id() == expected_thread_id:
                return True
            try:
                active = self._page.locator(
                    f"nav a[aria-current='page'][href^='/c/{expected_thread_id}'], "
                    f"nav a[data-active][href^='/c/{expected_thread_id}']"
                ).first
                if await active.count() and await active.is_visible():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def switch_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        conversation_url: str | None = None,
    ) -> None:
        """Switch via Recent history, then URL, then reload recovery."""
        if self._extract_thread_id() == conversation_id:
            await self.wait_chatgpt_ready()
            return

        if title:
            try:
                row = await self.find_conversation_row(
                    title, conversation_id=conversation_id, timeout=10_000
                )
                link = await self._conversation_link(row, conversation_id)
                await link.click()
                await self.wait_chatgpt_ready()
                if await self.verify_current_conversation(conversation_id):
                    return
            except Exception as error:
                log.info("Recent-list switch failed for %s: %s", conversation_id, error)

        target_url = conversation_url or f"{Config.CHATGPT_URL}/c/{conversation_id}"
        try:
            await self._page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
            await self.wait_chatgpt_ready()
            if await self.verify_current_conversation(conversation_id):
                return
        except Exception as error:
            log.warning("Direct conversation navigation failed for %s: %s", conversation_id, error)

        await self.recover_page(target_url, conversation_id)
        if not await self.verify_current_conversation(conversation_id):
            raise ConversationNotFoundError(
                f"Conversation switch verification failed: {conversation_id}"
            )

    async def rename_conversation(
        self,
        old_title: str | None,
        new_title: str,
        *,
        conversation_id: str | None = None,
    ) -> bool:
        """Rename only inside the exact matched conversation row."""
        row = await self.find_conversation_row(
            old_title, conversation_id=conversation_id, timeout=10_000
        )
        return await self._rename_conversation_row(row, new_title, conversation_id)

    async def _rename_conversation_row(
        self,
        row,
        new_title: str,
        conversation_id: str | None,
        *,
        allow_active: bool = False,
    ) -> bool:
        await row.hover()
        menu = None
        for selector in Selectors.CONVERSATION_MENU_BUTTON:
            candidate = row.locator(selector).first
            try:
                await candidate.wait_for(state="visible", timeout=1_500)
                menu = candidate
                break
            except Exception:
                continue
        if menu is None:
            raise RuntimeError("Conversation menu did not appear after hover")
        await menu.click()

        rename_item = None
        for selector in Selectors.RENAME_MENU_ITEM:
            candidate = self._page.locator(selector).last
            try:
                await candidate.wait_for(state="visible", timeout=1_500)
                rename_item = candidate
                break
            except Exception:
                continue
        if rename_item is None:
            raise RuntimeError("Rename menu item was not found")
        await rename_item.click()

        editor = None
        for selector in Selectors.RENAME_INPUT:
            candidate = self._page.locator(selector).last
            try:
                await candidate.wait_for(state="visible", timeout=1_500)
                editor = candidate
                break
            except Exception:
                continue
        if editor is None:
            raise RuntimeError("Rename input was not found")
        await editor.fill(new_title)
        await editor.press("Enter")
        try:
            await self.find_conversation_row(
                new_title,
                conversation_id=conversation_id,
                allow_active=allow_active,
                timeout=8_000,
            )
            return True
        except ConversationNotFoundError:
            return False

    async def rename_current_conversation(self, title: str) -> bool:
        """Rename the current ChatGPT sidebar conversation through the visible UI."""
        thread_id = await self.resolve_current_thread_id()
        if not thread_id:
            return False
        try:
            try:
                return await self.rename_conversation(None, title, conversation_id=thread_id)
            except ConversationNotFoundError:
                # New ChatGPT conversations may temporarily use /c/WEB:<id>
                # in the sidebar while the canonical ID is already in the
                # page URL. Only the active row is accepted in this fallback.
                row = await self.find_conversation_row(
                    None,
                    conversation_id=thread_id,
                    allow_active=True,
                    timeout=5_000,
                )
                return await self._rename_conversation_row(
                    row, title, thread_id, allow_active=True
                )
        except Exception as error:
            log.error("Conversation rename failed for %s: %s", thread_id, error)
            return False

    async def dump_buttons(self) -> list[dict]:
        """Return stable button attributes for selector diagnostics."""
        return await self._page.evaluate(
            """() => Array.from(document.querySelectorAll('button')).map((button, index) => ({
                index,
                text: (button.innerText || '').trim().slice(0, 160),
                ariaLabel: button.getAttribute('aria-label') || '',
                title: button.getAttribute('title') || '',
                role: button.getAttribute('role') || '',
                testId: button.getAttribute('data-testid') || ''
            }))"""
        )

    async def capture_debug_artifacts(self, task_id: str | None = None) -> dict[str, str]:
        """Save screenshot, HTML and current URL after a browser failure."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "browser")[:80]
        directory = Config.LOG_DIR / "browser_debug"
        directory.mkdir(parents=True, exist_ok=True)
        base = directory / f"{safe_id}-{stamp}"
        screenshot = base.with_suffix(".png")
        html = base.with_suffix(".html")
        try:
            await self._page.screenshot(path=str(screenshot), full_page=True)
            html.write_text(await self._page.content(), encoding="utf-8")
        except Exception as error:
            log.warning("Could not capture complete browser diagnostics: %s", error)
        log.error("Browser failure URL: %s", self._page.url)
        return {"screenshot": str(screenshot), "html": str(html), "url": self._page.url}

    # ── Private Helpers ─────────────────────────────────────────

    async def _extract_image_turn_text(self, previous_turn_signature: str | None = None) -> str:
        """
        Extract any text content from the latest turn (for image responses).

        Image turns may contain a title/description like:
        "Creating image • Adorable orange tabby kitten close-up"
        """
        text = await self._page.evaluate("""
            (previousSignature) => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                if (turns.length === 0) return '';

                let last = null;
                for (let idx = turns.length - 1; idx >= 0; idx--) {
                    const turn = turns[idx];
                    const turnRole = turn.getAttribute('data-turn');
                    const hasAssistantRole = turnRole === 'assistant' ||
                        Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                    if (!hasAssistantRole) continue;

                    const stableId =
                        turn.getAttribute('data-turn-id') ||
                        turn.getAttribute('data-testid') ||
                        turn.id ||
                        '';
                    const signature = `${idx}:${stableId}`;
                    if (previousSignature && signature === previousSignature) {
                        return '';
                    }

                    last = turn;
                    break;
                }

                if (!last) return '';

                // Try to get descriptive text (not "ChatGPT said:" heading)
                const spans = last.querySelectorAll('span');
                const parts = [];
                for (const span of spans) {
                    const t = (span.innerText || '').trim();
                    if (t && t.length > 3 && t.length < 300 &&
                        !t.includes('ChatGPT') && !t.includes('said')) {
                        parts.push(t);
                    }
                }
                if (parts.length > 0) return parts.join(' ');

                // Fallback: full turn inner text
                const full = (last.innerText || '').trim();
                // Strip the "ChatGPT said:" prefix
                return full.replace(/^ChatGPT said:\\s*/i, '').trim();
            }
        """, previous_turn_signature)
        return text or ""

    async def _find_selector(self, selectors: list[str], name: str) -> str | None:
        """
        Try each selector in the fallback list. Return the first one that matches.
        """
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

    async def _dismiss_overlays(self) -> None:
        """Check for and dismiss any blocking dialogs/overlays on the page."""
        try:
            result = await self._page.evaluate(
                """
                () => {
                    const info = { dismissed: [], found: [] };

                    // Check for role="dialog" overlays
                    const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog[open]');
                    for (const d of dialogs) {
                        const text = (d.innerText || '').trim().substring(0, 200);
                        info.found.push('dialog: ' + text);

                        // Try to find and click dismiss/close buttons
                        const closeBtn = d.querySelector(
                            'button[aria-label="Close"], button[aria-label="Dismiss"], ' +
                            'button:has(svg[data-testid="close"]), button.close'
                        );
                        if (closeBtn) {
                            closeBtn.click();
                            info.dismissed.push('dialog-close');
                        }
                    }

                    // Check for "Continue generating" button
                    const allButtons = document.querySelectorAll('button');
                    for (const btn of allButtons) {
                        const btnText = (btn.innerText || '').trim().toLowerCase();
                        if (btnText.includes('continue generating')) {
                            btn.click();
                            info.dismissed.push('continue-generating');
                        }
                    }

                    // Check for rate limit or error banners
                    const banners = document.querySelectorAll('[class*="banner"], [class*="toast"], [class*="alert"]');
                    for (const b of banners) {
                        const text = (b.innerText || '').trim().substring(0, 200);
                        if (text) info.found.push('banner: ' + text);
                    }

                    return info;
                }
                """
            )
            if result and isinstance(result, dict):
                if result.get("dismissed"):
                    log.info(f"Dismissed overlays: {result['dismissed']}")
                if result.get("found"):
                    log.debug(f"Page overlays found: {result['found']}")
        except Exception as e:
            log.debug(f"Overlay check failed: {e}")

    async def _click_send(self) -> bool:
        """Try to click the send button using selector fallbacks."""
        # Check send button state before clicking
        btn_state = await self._page.evaluate(
            """
            () => {
                const selectors = [
                    'button[data-testid="send-button"]',
                    '#composer-submit-button',
                    "button[aria-label='Send prompt']",
                ];
                for (const sel of selectors) {
                    const btn = document.querySelector(sel);
                    if (btn) {
                        return {
                            selector: sel,
                            disabled: btn.disabled,
                            ariaDisabled: btn.getAttribute('aria-disabled'),
                            visible: btn.offsetParent !== null,
                            classes: btn.className.substring(0, 100),
                        };
                    }
                }
                return null;
            }
            """
        )
        log.debug(f"Send button state: {btn_state}")

        # Don't click a disabled send button — the input wasn't recognized
        if isinstance(btn_state, dict) and btn_state.get("disabled"):
            log.warning("Send button is disabled — text may not have been inserted properly")
            return False

        selector = await self._find_selector(Selectors.SEND_BUTTON, "send button")
        if selector:
            await human_click(self._page, selector)
            log.info(f"Send button clicked via: {selector}")
            return True
        return False

    async def _wait_for_send_ready(self, timeout_ms: int) -> bool:
        """Wait until a visible send button is enabled."""
        elapsed = 0
        while elapsed < timeout_ms:
            try:
                ready = await self._page.evaluate(
                    """
                    () => {
                        const selectors = [
                            'button[data-testid="send-button"]',
                            '#composer-submit-button',
                            "button[aria-label='Send prompt']",
                        ];
                        for (const selector of selectors) {
                            const button = document.querySelector(selector);
                            if (!button || button.offsetParent === null) continue;
                            return !button.disabled && button.getAttribute('aria-disabled') !== 'true';
                        }
                        return false;
                    }
                    """
                )
                if ready:
                    return True
            except Exception as error:
                log.debug(f"Send readiness check failed: {error}")
            await asyncio.sleep(0.4)
            elapsed += 400
        log.warning(f"Send button did not become ready within {timeout_ms}ms")
        return False

    async def _composer_has_text(self) -> bool:
        """Check whether the composer input currently holds any text.

        Returns True when the check itself fails, so an inspection error can
        never block sending.
        """
        try:
            text = await self._page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '#prompt-textarea, textarea[name="prompt-textarea"], textarea[aria-label="Chat with ChatGPT"], div[contenteditable="true"]'
                    );
                    return el ? (el.value || el.innerText || el.textContent || '').trim() : '';
                }"""
            )
            return bool(text)
        except Exception:
            return True

    async def _upload_files(self, file_paths: list[str]) -> None:
        """
        Upload files (images, PDFs, docs, etc.) into ChatGPT's composer.

        ChatGPT keeps a hidden <input type="file"> in the composer. We set
        files on it directly. Because the DOM changes over time, this method:
          1. tries every known file-input selector,
          2. falls back to clicking the attach button (which can inject a
             fresh input) and re-querying,
          3. then waits for an attachment chip so we *confirm* the file was
             staged instead of blindly sleeping, and
          4. watches for upload-failure toasts and reports them.
        """
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

        log.info(f"Uploading {len(valid_paths)} file(s): {[Path(p).name for p in valid_paths]}")

        has_non_image = any(
            Path(p).suffix.lower() not in _IMAGE_EXTENSIONS for p in valid_paths
        )
        file_input = await self._find_file_input(skip_image_only=has_non_image)
        if file_input is None:
            # No input present — clicking the attach button often injects one.
            log.info("No file input found; clicking attach button to trigger injection...")
            await self._click_attach_button()
            await asyncio.sleep(1.0)
            file_input = await self._find_file_input(skip_image_only=has_non_image)

        if file_input is None:
            raise RuntimeError(
                "Could not locate a file-upload input in the ChatGPT composer; "
                "the DOM may have changed (see Selectors.FILE_UPLOAD_INPUT)."
            )

        try:
            await file_input.set_input_files(valid_paths)
            log.info(f"Set {len(valid_paths)} file(s) on file input")
        except Exception as e:
            log.error(f"set_input_files failed: {e}")
            raise RuntimeError(f"Could not upload files: {e}")

        # Every requested attachment must reach READY before prompt submission.
        failed: list[str] = []
        for path in valid_paths:
            filename = Path(path).name
            if not await self._wait_for_attachment(filename):
                failed.append(filename)
        if failed:
            raise AttachmentUploadError(failed)
        log.info("All %s attachment(s) are READY in the composer", len(valid_paths))
        # Let the composer finish processing the staged upload. Typing
        # immediately after the chip appears can be silently dropped while
        # the upload is still being registered.
        await asyncio.sleep(2.0)

    async def _find_file_input(self, skip_image_only: bool = False):
        """Return the first suitable file-input element, or None.

        With skip_image_only=True, inputs whose accept attribute restricts
        them to images (e.g. #upload-photos, #upload-camera) are skipped —
        ChatGPT silently ignores non-image files set on those inputs.
        """
        for selector in Selectors.FILE_UPLOAD_INPUT:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    if not el:
                        continue
                    if skip_image_only:
                        accept = (await el.get_attribute("accept") or "").lower()
                        if _accept_is_image_only(accept):
                            log.debug(f"Skipping image-only input: {selector}")
                            continue
                    log.debug(f"Found file input via: {selector}")
                    return el
            except Exception:
                continue
        return None

    async def _click_attach_button(self) -> bool:
        """Click the composer attach/plus button if present."""
        for selector in Selectors.ATTACH_BUTTON:
            try:
                btn = await self._page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.debug(f"Clicked attach button via: {selector}")
                    return True
            except Exception:
                continue
        log.debug("No attach button found")
        return False

    async def _wait_for_attachment(self, filename: str, timeout_ms: int = 8000) -> bool:
        """
        Poll until the staged attachment is visible in the composer, or an
        upload-failure toast appears. Detection is generic: we look for the
        filename showing up in any visible element (text, aria-label, title,
        alt) rather than depending on specific chip class names, which
        ChatGPT changes frequently.
        """
        deadline = time.time() + timeout_ms / 1000
        # ChatGPT sometimes truncates long names in the pill; match on stem too.
        stem = Path(filename).stem.lower()
        while time.time() < deadline:
            try:
                state = await self._page.evaluate(
                    """
                    (args) => {
                        const { name, stem } = args;
                        let nameMatch = false;
                        // Generic scan: any visible element mentioning the file.
                        const els = document.querySelectorAll('body *');
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            if (rect.height > 400) continue;  // skip big containers
                            const hay = (
                                (el.innerText || '') + ' ' +
                                (el.getAttribute('aria-label') || '') + ' ' +
                                (el.getAttribute('title') || '') + ' ' +
                                (el.getAttribute('alt') || '')
                            ).toLowerCase();
                            if (hay.includes(name) || (stem.length > 3 && hay.includes(stem))) {
                                nameMatch = true;
                                break;
                            }
                        }
                        // Detect upload-failure toasts.
                        let failed = false;
                        const toasts = document.querySelectorAll("[role='alert'], [class*='toast'], [class*='Toastify']");
                        for (const t of toasts) {
                            const txt = (t.innerText || '').toLowerCase();
                            if (txt.includes('failed') || txt.includes("couldn't upload") || txt.includes('too large')) {
                                failed = true;
                            }
                        }
                        return { nameMatch, failed };
                    }
                    """,
                    {"name": filename.lower(), "stem": stem},
                )
                if state.get("failed"):
                    log.error("ChatGPT reported an upload failure toast")
                    return False
                if state.get("nameMatch"):
                    return True
            except Exception as e:
                log.debug(f"Attachment poll error: {e}")
            await asyncio.sleep(0.4)
        return False

    def _extract_thread_id(self) -> str:
        """Extract the thread/conversation ID from the current URL."""
        url = self._page.url
        match = re.search(r"/c/([A-Za-z0-9-]+)", url)
        return match.group(1) if match else ""
