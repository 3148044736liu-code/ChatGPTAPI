from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.browser.human import human_type
from src.chatgpt.client import ChatGPTClient
from src.selectors import Selectors


class _Textarea:
    def __init__(self) -> None:
        self.first = self
        self.value = ""
        self.click = AsyncMock()
        self.fill = AsyncMock(side_effect=self._fill)
        self.evaluate = AsyncMock(return_value=True)

    async def _fill(self, value: str) -> None:
        self.value = value


class _TextareaPage:
    def __init__(self) -> None:
        self.element = _Textarea()
        self.evaluate = AsyncMock()

    def locator(self, _selector: str) -> _Textarea:
        return self.element


@pytest.mark.asyncio
async def test_human_type_fills_native_textarea() -> None:
    page = _TextareaPage()

    await human_type(page, "textarea[name='prompt-textarea']", "hello")

    page.element.fill.assert_awaited_once_with("hello")
    assert page.element.value == "hello"
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_composer_text_check_supports_textarea_value() -> None:
    page = type("Page", (), {})()
    page.evaluate = AsyncMock(return_value="hello")
    client = object.__new__(ChatGPTClient)
    client._page = page

    assert await client._composer_has_text() is True
    script = page.evaluate.await_args.args[0]
    assert 'textarea[name="prompt-textarea"]' in script
    assert "el.value" in script


def test_chat_input_selectors_include_current_textarea() -> None:
    assert "textarea[name='prompt-textarea']" in Selectors.CHAT_INPUT

