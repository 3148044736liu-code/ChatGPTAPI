"""Concurrent-submit scheduler backed by exactly one persistent browser page."""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.api.conversation_router import ConversationRouter
from src.api.risk_controller import risk_controller
from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.config import Config
from src.files.generated import NetworkFileCapture, capture_generated_files
from src.log import setup_logging
from src.provider_errors import ProviderStateUnknownError

log = setup_logging("browser_scheduler")


class BrowserQueueFullError(RuntimeError):
    pass


@dataclass
class BrowserWorker:
    id: int
    client: ChatGPTClient | ClaudeClient


@dataclass
class BrowserJob:
    future: asyncio.Future
    session_id: str
    provider_thread_id: str | None
    provider_thread_url: str | None
    message: str
    file_paths: list[str]
    generated_dir: Path
    task_id: str | None = None
    request_id: str | None = None


class SessionWorkerPool:
    """FIFO scheduler: many HTTP submitters, one browser execution channel."""

    def __init__(self, size: int | None = None) -> None:
        self.size = 1
        self._queue: asyncio.Queue[BrowserJob] = asyncio.Queue(maxsize=Config.MAX_BROWSER_QUEUE_DEPTH)
        self._worker: BrowserWorker | None = None
        self._browser: BrowserManager | None = None
        self._runner: asyncio.Task | None = None
        self._busy = False
        self._router = ConversationRouter()
        self._operation_lock = asyncio.Lock()
        self._last_started_monotonic = 0.0
        self._last_sampled_gap = 0.0
        self._next_not_before_monotonic = 0.0
        self.current_task_id: str | None = None
        self.current_request_id: str | None = None
        self._cancelled: set[str] = set()

    async def start(self, browser: BrowserManager) -> None:
        self._browser = browser
        page = browser.page
        # A single account must never retain extra task/session tabs.
        for candidate in list(browser.context.pages):
            if candidate is not page and not candidate.is_closed():
                await candidate.close()
        self._worker = BrowserWorker(1, self._client_for(page))
        self._runner = asyncio.create_task(self._run(), name="single-page-browser-scheduler")
        log.info("Browser scheduler ready with exactly one persistent page")

    @staticmethod
    def _client_for(page):
        if Config.PROVIDER == "claude":
            return ClaudeClient(page, use_clipboard=False)
        return ChatGPTClient(page, use_clipboard=False)

    async def _ensure_alive(self) -> None:
        if self._worker is None or self._browser is None:
            raise RuntimeError("SessionWorkerPool.start() must be called first")
        if not self._worker.client.page.is_closed():
            return
        # The old page is already closed, so creating this replacement still
        # preserves the one-live-page invariant.
        page = await self._browser.context.new_page()
        self._browser._page = page
        await page.goto(Config.provider_url(), wait_until="domcontentloaded")
        self._worker.client = self._client_for(page)
        log.warning("Replaced a closed browser page; no in-flight request was retried")

    async def close(self) -> None:
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        self._runner = None
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if not job.future.done():
                job.future.set_exception(RuntimeError("Browser scheduler stopped"))
            self._queue.task_done()
        self._worker = None

    @property
    def available(self) -> int:
        return 0 if self._busy else 1

    @property
    def healthy(self) -> int:
        try:
            return int(self._worker is not None and not self._worker.client.page.is_closed())
        except Exception:
            return 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def last_sampled_gap_seconds(self) -> float:
        return self._last_sampled_gap

    @property
    def next_not_before(self) -> float | None:
        if not self._next_not_before_monotonic:
            return None
        return time.time() + max(0.0, self._next_not_before_monotonic - time.monotonic())

    async def repair_closed_workers(self) -> int:
        if not self._busy:
            await self._ensure_alive()
        return self.healthy

    async def _wait_start_gap(self) -> None:
        if not self._last_started_monotonic:
            return
        self._last_sampled_gap = random.uniform(
            Config.BROWSER_TASK_GAP_MIN_SECONDS,
            Config.BROWSER_TASK_GAP_MAX_SECONDS,
        )
        self._next_not_before_monotonic = self._last_started_monotonic + self._last_sampled_gap
        delay = self._next_not_before_monotonic - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _execute(self, job: BrowserJob):
        await self._ensure_alive()
        assert self._worker is not None
        client = self._worker.client
        await self._router.prepare(
            client,
            task_id=job.task_id,
            provider_thread_id=job.provider_thread_id,
            provider_thread_url=job.provider_thread_url,
        )
        # This second check is intentionally adjacent to send_message. It
        # prevents future preparation hooks from switching the shared page
        # after routing but before composer input.
        await self._router.verify(client, job.provider_thread_id)
        # NetworkFileCapture is intentionally a *fallback* in the new
        # architecture — it only helps when the DOM-click path misses
        # (e.g. ChatGPT triggers a JS fetch + blob that does not fire a
        # real download event).  The DOM pipeline runs regardless.
        net_capture = NetworkFileCapture(client.page)
        net_capture.start()
        try:
            try:
                result = await client.send_message(job.message, file_paths=job.file_paths or None)
            except Exception as error:
                try:
                    dead = client.page.is_closed()
                except Exception:
                    dead = True
                if dead:
                    raise ProviderStateUnknownError(
                        "Browser page closed after request execution began; delivery state is unknown"
                    ) from error
                raise
            if job.task_id and not job.provider_thread_id:
                if not result.thread_id:
                    resolver = getattr(client, "resolve_current_thread_id", None)
                    if resolver is not None:
                        result.thread_id = await resolver()
                if not result.thread_id:
                    raise RuntimeError("Provider did not expose a conversation ID after sending")
                await self._router.bind_created(
                    client,
                    task_id=job.task_id,
                    provider_thread_id=result.thread_id,
                )
            if result.thread_id and not result.thread_url:
                result.thread_url = getattr(client.page, "url", "")
            generated = await capture_generated_files(
                client.page,
                job.generated_dir,
                network_capture=net_capture,
            )
            return result, generated
        finally:
            net_capture.stop()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            self._busy = True
            self.current_task_id = job.task_id
            self.current_request_id = job.request_id
            try:
                if job.request_id and job.request_id in self._cancelled:
                    self._cancelled.discard(job.request_id)
                    if not job.future.done():
                        job.future.cancel()
                    continue
                risk_controller.ensure_available()
                await self._wait_start_gap()
                self._last_started_monotonic = time.monotonic()
                self._next_not_before_monotonic = 0.0
                async with self._operation_lock:
                    result = await self._execute(job)
                if not job.future.done():
                    job.future.set_result(result)
            except Exception as error:
                risk_controller.observe_exception(error)
                if self._worker is not None:
                    capture = getattr(self._worker.client, "capture_debug_artifacts", None)
                    if capture is not None:
                        try:
                            await capture(job.task_id)
                        except Exception as capture_error:
                            log.warning("Browser diagnostics failed: %s", capture_error)
                if not job.future.done():
                    job.future.set_exception(error)
            finally:
                self._busy = False
                self.current_task_id = None
                self.current_request_id = None
                self._queue.task_done()

    async def send(
        self,
        *,
        session_id: str,
        provider_thread_id: str | None,
        message: str,
        file_paths: list[str],
        generated_dir: Path,
        provider_thread_url: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
    ):
        risk_controller.ensure_available()
        if self._runner is None or self._runner.done():
            raise RuntimeError("Browser scheduler is not running")
        if self._queue.full():
            raise BrowserQueueFullError("Browser scheduler queue is full")
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(
            BrowserJob(
                future=future,
                session_id=session_id,
                provider_thread_id=provider_thread_id,
                provider_thread_url=provider_thread_url,
                message=message,
                file_paths=list(file_paths),
                generated_dir=generated_dir,
                task_id=task_id,
                request_id=request_id,
            )
        )
        return await future

    def cancel(self, request_id: str) -> bool:
        if self.current_request_id == request_id:
            return False
        if not request_id:
            return False
        self._cancelled.add(request_id)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "capacity": 1,
            "available": self.available,
            "busy": int(self._busy),
            "queue_depth": self.queue_depth,
            "queue_capacity": Config.MAX_BROWSER_QUEUE_DEPTH,
            "current_task_id": self.current_task_id,
            "current_request_id": self.current_request_id,
            "last_sampled_gap_seconds": self._last_sampled_gap or None,
            "next_not_before": self.next_not_before,
            "single_page": True,
            "risk": risk_controller.snapshot(),
        }


def safe_owner_segment(owner_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", owner_id)[:80] or "default"
