"""
Centralized DOM selectors for ChatGPT.

All selectors live here so when ChatGPT updates their UI, we only
change this one file. Each entry is a list of fallback selectors —
try them in order until one matches.
"""

from __future__ import annotations


class Selectors:
    """CSS / Playwright selectors for chatgpt.com UI elements."""

    # ── Chat input ──────────────────────────────────────────────
    CHAT_INPUT = [
        "#prompt-textarea",
        "div[contenteditable='true'][id='prompt-textarea']",
        "div[contenteditable='true']",
    ]

    # ── Send button ─────────────────────────────────────────────
    SEND_BUTTON = [
        "button[data-testid='send-button']",
        "#composer-submit-button",
        "button[aria-label='Send prompt']",
        "#prompt-textarea ~ button",
    ]

    # ── Assistant response messages ─────────────────────────────
    ASSISTANT_MESSAGE = [
        "div[data-message-author-role='assistant']",
        "[data-message-author-role='assistant']",
        "section[data-turn='assistant']",
        "section[data-testid^='conversation-turn-']",
    ]

    # ── Streaming / stop button (visible while generating) ─────
    STOP_BUTTON = [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop answering']",
        "button[aria-label='Stop generating']",
    ]

    # ── New chat ────────────────────────────────────────────────
    NEW_CHAT_BUTTON = [
        "a[data-testid='create-new-chat-button']",
        "a[href='/']",
        "nav a[href='/']",
    ]

    # ── Sidebar conversation links ──────────────────────────────
    SIDEBAR_THREAD_LINKS = [
        "nav a[href^='/c/']",
        "a[href^='/c/']",
    ]

    # Conversation controls are scoped to the matched sidebar row. ChatGPT
    # inserts some of these buttons only after hover, so callers must wait for
    # visibility before clicking.
    CONVERSATION_MENU_BUTTON = [
        "button[aria-label='Open conversation options']",
        "button[aria-label*='conversation options' i]",
        "button[aria-label*='menu' i]",
        "button[data-testid*='conversation'][data-testid*='menu']",
        "button[data-testid*='menu']",
    ]

    RENAME_MENU_ITEM = [
        "[role='menuitem']:has-text('Rename')",
        "[role='menuitem']:has-text('重命名')",
        "button:has-text('Rename')",
        "button:has-text('重命名')",
    ]

    RENAME_INPUT = [
        "[role='dialog'] input",
        "input[aria-label*='title' i]",
        "input[placeholder*='title' i]",
    ]

    # ── Login page detection (if any of these appear, user is logged out) ──
    LOGIN_INDICATORS = [
        "button[data-testid='login-button']",
        "button:has-text('Log in')",
        "[data-testid='login-button']",
    ]

    # ── Markdown content inside assistant message ───────────────
    ASSISTANT_MARKDOWN = [
        "div[data-message-author-role='assistant'] .markdown",
        "div[data-message-author-role='assistant'] .prose",
        "section[data-turn='assistant'] .markdown",
        "section[data-turn='assistant'] .prose",
    ]

    # ── Regenerate / continue buttons (appear after response completes) ──
    POST_RESPONSE_BUTTONS = [
        "button:has-text('Regenerate')",
        "button:has-text('Continue generating')",
    ]

    # ── Copy button (appears on each completed assistant message) ──────
    # This is the most reliable completion signal — it only appears
    # after the full response has been generated.
    COPY_BUTTON = [
        "button[data-testid='copy-turn-action-button']",
        "button[aria-label='Copy message']",
        "button[aria-label='Copy']",
    ]

    # ── Generated images inside assistant responses ───────────────────
    # ChatGPT DALL-E image responses do NOT have data-message-author-role.
    # Instead, the image lives inside an article turn with class "agent-turn".
    # Images have alt="Generated image" and src from chatgpt.com/backend-api.
    # Image wrapper DIVs have id="image-{uuid}" and class group/imagegen-image.
    ASSISTANT_IMAGE = [
        "img[alt='Generated image']",
        "div[id^='image-'] img",
        "section[data-turn='assistant'] img[alt='Generated image']",
    ]

    # Image container identifiers (used for detection, not clicking)
    IMAGE_CONTAINER = [
        "div[id^='image-']",
        "div[class*='imagegen-image']",
    ]

    # Download button for generated images
    IMAGE_DOWNLOAD_BUTTON = [
        "a[aria-label='Download']",
        "a[download]",
    ]

    # ── File / attachment upload input ────────────────────────────
    # ChatGPT's composer renders THREE file inputs (verified 2026-08):
    #   input#upload-files   — generic, no accept restriction, inside the
    #                          composer <form>. This is the one that must
    #                          receive documents (txt/pdf/docx/...).
    #   input#upload-photos  — accept="image/*", image-only.
    #   input#upload-camera  — accept="image/*" capture, camera-only.
    # IMPORTANT: setting a non-image file on the image-only inputs silently
    # does nothing (no chip appears, ChatGPT never sees the file).
    FILE_UPLOAD_INPUT = [
        "input#upload-files",
        "input[data-testid='upload-files-input']",
        "input[data-testid='file-upload']",
        "input#upload-photos",
        "input[data-testid='upload-photos-input']",
        "input[type='file']",
    ]

    # Attach / upload button (opens the file picker). Clicking this can
    # inject a fresh <input type="file"> when one is not already present.
    ATTACH_BUTTON = [
        "button[data-testid='composer-attach-button']",
        "button[data-testid='attach-button']",
        "button[data-testid='composer-plus-btn']",
        "button[aria-label='Add files and more']",
        "button[aria-label='Attach files']",
        "button[aria-label*='Attach']",
    ]

    # Attachment chip / badge shown in the composer once a file has been
    # staged. Presence of one of these is our "upload finished" signal.
    ATTACHMENT_CHIP = [
        "[data-testid='attachment-chip']",
        "[data-testid*='attachment']",
        "[data-testid='composer-attachment']",
        "div[class*='attachment']",
        "[class*='file-pill']",
        "[class*='attachment-pill']",
    ]
