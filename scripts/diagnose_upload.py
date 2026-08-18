"""One-shot diagnostic: inspect ChatGPT composer upload affordances.

Run while the API server is STOPPED (browser_data profile is exclusive).

It launches the logged-in persistent profile, opens chatgpt.com, then:
  1. dumps every <input type="file"> (attributes + visibility),
  2. dumps candidate attach/plus buttons,
  3. clicks the attach button and reports whether a native file chooser
     fires immediately, or a menu opens (and lists the menu items).

Output is plain ASCII so it is safe under the GBK console.
"""

from __future__ import annotations

import asyncio
import io
import sys

# Force UTF-8 output regardless of console codepage.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.browser.manager import BrowserManager
from src.config import Config


async def with_timeout(coro, seconds=15, label="step"):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        print(f"[timeout] {label} exceeded {seconds}s")
        return None


async def main() -> None:
    browser = BrowserManager()
    page = await browser.start()
    await browser.navigate(Config.CHATGPT_URL)
    await browser.apply_stealth_patches()
    await asyncio.sleep(4)

    print("=== URL:", page.url)

    # 1) All file inputs
    inputs = await with_timeout(
        page.evaluate(
            """
            () => {
                const list = [];
                document.querySelectorAll('input[type=file]').forEach((el, i) => {
                    const rect = el.getBoundingClientRect();
                    list.push({
                        index: i,
                        id: el.id || '',
                        name: el.name || '',
                        accept: el.getAttribute('accept') || '',
                        multiple: el.multiple,
                        testid: el.getAttribute('data-testid') || '',
                        visible: rect.width > 0 && rect.height > 0,
                        disabled: el.disabled,
                        inForm: !!el.closest('form'),
                        parentTag: el.parentElement ? el.parentElement.tagName : '',
                        outer: el.outerHTML.slice(0, 180),
                    });
                });
                return list;
            }
            """
        ),
        label="dump file inputs",
    )
    print(f"=== file inputs found: {len(inputs) if inputs else 0}")
    for it in inputs or []:
        print(it)

    # 2) Candidate attach buttons
    buttons = await with_timeout(
        page.evaluate(
            """
            () => {
                const found = [];
                const sels = [
                    "button[data-testid='composer-attach-button']",
                    "button[data-testid='attach-button']",
                    "button[data-testid='composer-plus-btn']",
                    "button[aria-label='Add files and more']",
                    "button[aria-label='Attach files']",
                ];
                sels.forEach(sel => {
                    document.querySelectorAll(sel).forEach(btn => {
                        const rect = btn.getBoundingClientRect();
                        found.push({
                            selector: sel,
                            testid: btn.getAttribute('data-testid') || '',
                            ariaLabel: btn.getAttribute('aria-label') || '',
                            visible: rect.width > 0 && rect.height > 0,
                            text: (btn.innerText || '').trim().slice(0, 40),
                        });
                    });
                });
                return found;
            }
            """
        ),
        label="dump attach buttons",
    )
    print(f"=== attach buttons found: {len(buttons) if buttons else 0}")
    for b in buttons or []:
        print(b)

    # 3) Click attach and see what happens (chooser vs menu)
    chooser_opened = {"value": False}

    def on_file_chooser(chooser):
        chooser_opened["value"] = True
        print("=== EVENT: native file chooser fired:", chooser.is_multiple())

    page.on("filechooser", on_file_chooser)

    clicked = await with_timeout(
        page.evaluate(
            """
            () => {
                const sels = [
                    "button[data-testid='composer-attach-button']",
                    "button[data-testid='attach-button']",
                    "button[data-testid='composer-plus-btn']",
                    "button[aria-label='Add files and more']",
                    "button[aria-label='Attach files']",
                ];
                for (const sel of sels) {
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); return sel; }
                }
                return null;
            }
            """
        ),
        label="click attach",
    )
    print("=== clicked attach selector:", clicked)
    await asyncio.sleep(1.5)

    if chooser_opened["value"]:
        print("=== RESULT: attach button opens a NATIVE FILE CHOOSER directly")
    else:
        # A menu likely opened — list visible menu items
        menu = await with_timeout(
            page.evaluate(
                """
                () => {
                    const items = [];
                    document.querySelectorAll('[role=menuitem], [role=menu] *, [data-radix-menu-content] *, [role=dialog] button').forEach(el => {
                        const rect = el.getBoundingClientRect();
                        const txt = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        if (rect.width > 0 && rect.height > 0 && txt && txt.length < 60) {
                            items.push(txt);
                        }
                    });
                    return [...new Set(items)].slice(0, 40);
                }
                """
            ),
            label="dump menu items",
        )
        print("=== RESULT: no immediate chooser; menu/dialog items visible:")
        for m in menu or []:
            print("   -", m)

    await browser.close()
    print("=== diagnostic done")


if __name__ == "__main__":
    asyncio.run(main())
