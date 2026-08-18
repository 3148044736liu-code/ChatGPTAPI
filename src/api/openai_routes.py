"""
OpenAI-compatible API routes.

Provides:
  POST /v1/chat/completions   — chat completions (with tool/function calling)
  GET  /v1/models             — list available models

All requests are serialized through an asyncio.Lock because the underlying
Playwright browser page is single-threaded.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCallInfo,
    FunctionDefinition,
    ImageData,
    ImageGenerationRequest,
    ImagesResponse,
    ModelListResponse,
    ModelObject,
    ResponseFunctionCall,
    ResponseObject,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponsesRequest,
    ResponseUsage,
    ToolCall,
    ToolDefinition,
    UsageInfo,
)
from src.chatgpt.client import ChatGPTClient
from src.claude.client import ClaudeClient
from src.config import Config
from src.api.session_service import safe_owner_segment
from src.api.managed_routes import _file_payload
from src.files.service import FileService, FileServiceError
from src.capabilities import authorize_capability
from src.api.errors import api_error, provider_error
from src.provider_errors import ProviderError, ProviderStateUnknownError
from src.log import setup_logging

log = setup_logging("openai_routes")

openai_router = APIRouter(tags=["OpenAI 兼容接口"])


def _model_dump_compat(model) -> dict:
    """Serialize Pydantic models on both the deployed v1 and future v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return dict(model)

# Global reference — set by server.py at startup
_client: ChatGPTClient | ClaudeClient | None = None

# Managed-session services (database + worker pool) — optional. When set,
# /v1/chat/completions can route through the multi-session worker pool by
# supplying session_id (or when Config.OPENAI_USE_SESSION_POOL is enabled).
_database = None
_session_pool = None
_file_service: FileService | None = None


def set_openai_services(database, pool) -> None:
    """Called by server.py to inject managed-session services."""
    global _database, _session_pool, _file_service
    _database = database
    _session_pool = pool
    _file_service = FileService(database) if database is not None else None

# Serialize all requests — single browser page, not thread-safe.
# Created lazily to avoid Python 3.9 event-loop binding issues.
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Get or create the global request lock (lazy init)."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# Track messages in the current thread to prevent thread exhaustion
_thread_message_count = 0
_MAX_THREAD_MESSAGES = 8  # Start a new chat after this many requests
_last_response_time: float = 0.0
_MIN_MESSAGE_GAP = 3.0  # Minimum seconds between messages (ChatGPT needs cooldown)


async def _ensure_fresh_chat() -> None:
    """Enforce cooldown between messages and start new chat if thread is full.

    ChatGPT's web UI degrades after ~6-8 messages in a thread (stops
    generating, copy-button never appears). We preemptively start a
    new chat after _MAX_THREAD_MESSAGES to prevent this.

    Also enforces a minimum gap between consecutive messages, since
    ChatGPT's UI may not accept rapid-fire messages properly.
    """
    global _thread_message_count, _last_response_time

    # Enforce minimum gap between messages
    if _last_response_time > 0:
        elapsed = time.time() - _last_response_time
        if elapsed < _MIN_MESSAGE_GAP:
            wait = _MIN_MESSAGE_GAP - elapsed
            log.debug(f"Cooldown: waiting {wait:.1f}s before next message")
            await asyncio.sleep(wait)

    if _thread_message_count < _MAX_THREAD_MESSAGES:
        return  # Thread is fresh enough — no navigation needed

    client = _get_client()
    try:
        await client.new_chat()
        _thread_message_count = 0
    except Exception as e:
        log.warning(f"new_chat() failed, retrying once: {e}")
        try:
            await asyncio.sleep(2)
            await client.new_chat()
            _thread_message_count = 0
        except Exception as e2:
            log.error(f"new_chat() retry also failed: {e2}")
            # Don't raise — continue with current thread rather than failing
            log.warning("Continuing with current thread despite new_chat failure")


def _increment_thread_count() -> None:
    """Increment the thread message counter after a successful response."""
    global _thread_message_count, _last_response_time
    _thread_message_count += 1
    _last_response_time = time.time()
    log.debug(f"Thread message count: {_thread_message_count}/{_MAX_THREAD_MESSAGES}")


def _get_model_id() -> str:
    """Return model ID based on active provider."""
    if Config.PROVIDER == "claude":
        return "claude-browser"
    return "catgpt-browser"


def set_openai_client(client: ChatGPTClient | ClaudeClient | None) -> None:
    """Called by server.py to inject the client."""
    global _client
    _client = client


def _get_client() -> ChatGPTClient | ClaudeClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Client not initialized")
    return _client


# ── Helpers ─────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return max(1, len(text) // 4)


def _extract_content_text(content) -> str:
    """Extract text from message content (handles both string and list format)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts) if parts else ""
    return str(content)


def _extract_image_urls(content) -> list[str]:
    """Extract image URLs from message content (OpenAI vision format)."""
    if not isinstance(content, list):
        return []
    urls = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            image_url = item.get("image_url", {})
            if isinstance(image_url, dict):
                url = image_url.get("url", "")
            else:
                url = str(image_url)
            if url:
                urls.append(url)
    return urls


def _extract_file_attachments(content) -> list[dict]:
    """
    Extract file attachments from message content.

    Supported content part format:
      {"type": "file", "file": {"filename": "test.pdf", "data": "base64...", "mime_type": "application/pdf"}}

    Also supports a shorthand data-URL style:
      {"type": "file", "file": {"filename": "test.pdf", "url": "data:application/pdf;base64,..."}}

    Returns list of dicts: [{"filename": str, "data_b64": str, "mime_type": str}, ...]
    """
    if not isinstance(content, list):
        return []
    files = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            continue
        filename = file_info.get("filename", "attachment")
        file_id = file_info.get("file_id")
        # Two ways to supply file data:
        # 1. data + mime_type  2. url (data-URL)
        data_b64 = file_info.get("data")
        mime_type = file_info.get("mime_type", "application/octet-stream")
        url = file_info.get("url", "")
        if not data_b64 and url.startswith("data:"):
            # Parse data URL
            try:
                header, data_b64 = url.split(",", 1)
                # header = "data:application/pdf;base64"
                if ":" in header and ";" in header:
                    mime_type = header.split(":")[1].split(";")[0]
            except ValueError:
                continue
        if file_id:
            files.append({"file_id": file_id})
        elif data_b64:
            files.append({"filename": filename, "data_b64": data_b64, "mime_type": mime_type})
        elif url:
            files.append({"filename": filename, "url": url, "mime_type": mime_type})
    return files


async def _download_file(
    url_or_data: str | dict,
    *,
    owner_id: str,
    session_id: str | None,
    require_session_match: bool = True,
) -> str:
    """Resolve an API attachment through the managed FileService."""
    if _file_service is None:
        raise HTTPException(status_code=503, detail="File service not initialized")
    try:
        record = await _file_service.create_from_source(
            url_or_data,
            owner_id=owner_id,
            session_id=session_id,
            require_session_match=require_session_match,
        )
    except FileServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"type": error.code.lower(), "message": str(error)},
        ) from error
    return record["stored_path"]


def _build_prompt(messages: list[ChatMessage]) -> str:
    """
    Flatten an OpenAI-style message array into a single prompt string
    that we can paste into ChatGPT's input box.

    The browser already maintains conversation context within a thread,
    so for simple single-turn calls we just send the last user message.
    For multi-turn with system prompts or tool results, we build a
    formatted transcript.
    """
    # Simple case: only one user message (and optionally one system message)
    non_system = [m for m in messages if m.role != "system"]
    system_msgs = [m for m in messages if m.role == "system"]

    # If it's just one user message, send it directly
    if len(non_system) == 1 and non_system[0].role == "user":
        prefix = ""
        if system_msgs:
            sys_text = _extract_content_text(system_msgs[0].content)
            if Config.PROVIDER == "claude":
                # Claude rejects "[System instruction: ...]" as prompt injection.
                # Present it as context instead.
                prefix = f"{sys_text}\n\n"
            else:
                prefix = f"[System instruction: {sys_text}]\n\n"
        user_text = _extract_content_text(non_system[0].content)
        return prefix + (user_text or "")

    # Multi-turn: build a transcript
    parts: list[str] = []
    for msg in messages:
        role = msg.role.capitalize()
        if msg.role == "system":
            if Config.PROVIDER == "claude":
                # For Claude, present system messages as context without the label
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(text)
            else:
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(f"System: {text}")
        elif msg.role == "tool":
            # Tool result — include both the call context and the result
            tool_content = _extract_content_text(msg.content)
            if Config.PROVIDER == "claude":
                parts.append(
                    f"The tool was executed and returned this result:\n{tool_content}\n\n"
                    f"Now use the result above to answer the user's original question in plain text."
                )
            else:
                parts.append(
                    f"[Tool result for {msg.tool_call_id or 'unknown'}]: {tool_content}\n\n"
                    f"Use the tool result to answer the user. Do NOT call tools again."
                )
        elif msg.role == "assistant" and msg.tool_calls:
            # Assistant requested tool calls — show what was called
            calls_desc = []
            for tc in msg.tool_calls:
                calls_desc.append(
                    f'{tc.function.name}({tc.function.arguments})'
                )
            parts.append(f"Assistant called tools: {', '.join(calls_desc)}")
        elif msg.content:
            text = _extract_content_text(msg.content)
            if text:
                parts.append(f"{role}: {text}")

    return "\n\n".join(parts)


def _build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict | None = None,
) -> str:
    """
    Build a system-level instruction that tells the model about available tools.

    *tool_choice* controls how insistent the instructions are:
      - "auto" / None  — model decides whether to call a tool or answer directly
      - "required"     — model MUST call at least one tool
      - "none"         — caller should not call this function at all
      - {"type":"function","function":{"name":"X"}} — model MUST call that tool
    """
    tool_descriptions = []
    for tool in tools:
        fn = tool.function
        desc = {
            "name": fn.name,
            "description": fn.description,
            "parameters": fn.parameters,
        }
        tool_descriptions.append(json.dumps(desc, indent=2))

    tools_json = "\n---\n".join(tool_descriptions)

    # ── Determine the decision instruction based on tool_choice ──
    forced_tool_name = None
    if isinstance(tool_choice, dict):
        # {"type": "function", "function": {"name": "X"}}
        forced_tool_name = (
            tool_choice.get("function", {}).get("name")
            if isinstance(tool_choice.get("function"), dict)
            else None
        )

    if forced_tool_name:
        decision = (
            f"You MUST call the function `{forced_tool_name}`. "
            f"Do NOT answer the question yourself — output only the JSON tool call."
        )
    elif tool_choice == "required":
        decision = (
            "You MUST call at least one of the available functions. "
            "Do NOT answer the question yourself — always output tool calls."
        )
    else:
        # "auto" or None — model decides
        decision = (
            "If the user's request can be fulfilled or assisted by one or more "
            "of the available functions, call the appropriate tool(s). "
            "If none of the tools are relevant, answer the user normally in plain text."
        )

    # ── Provider-specific prompt framing ──
    if Config.PROVIDER == "claude":
        return f"""You have access to external tools through a structured interface. {decision}

When calling tools, respond with ONLY a JSON code block — no text before or after it:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. Do not add any commentary, explanation, or text outside the code block.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types shown in each function's schema.
4. When you receive tool results in a follow-up message, use them to give the user a natural, helpful answer. Do NOT output another JSON tool call for the same request.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""
    else:
        return f"""You are in tool-calling mode. {decision}

When calling tools, output ONLY a JSON code block — no other text:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. No explanation, no text before or after.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types from each function's schema.
4. When a follow-up message contains tool results, summarize them naturally for the user. Do NOT call tools again for the same request.
5. Do not refuse or say tools are unavailable — they are available through this interface.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""


def _extract_json_object(text: str, anchor: str = "tool_calls") -> str | None:
    """
    Extract a JSON object containing *anchor* key from *text*.

    Uses two strategies:
      1. Look inside markdown code blocks (```json ... ```)
      2. Find the anchor key and walk outward using brace-depth tracking
         to handle arbitrarily nested JSON (arrays, nested objects, etc.)
    """
    # Strategy 1: code blocks — most reliable when the model obeys the prompt
    for m in re.finditer(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```", text):
        candidate = m.group(1).strip()
        if anchor in candidate:
            try:
                parsed = json.loads(candidate)
                if anchor in parsed:
                    return candidate
            except json.JSONDecodeError:
                continue

    # Strategy 2: locate anchor, walk to balanced braces
    search_key = f'"{anchor}"'
    idx = text.find(search_key)
    if idx == -1:
        return None

    # Walk backward to the nearest '{'
    start = text.rfind("{", 0, idx)
    if start == -1:
        return None

    # Walk forward tracking brace depth, respecting JSON string literals
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2          # skip escaped char
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        return None
        i += 1

    return None


def _parse_tool_calls(
    response_text: str, tools: list[ToolDefinition]
) -> list[ToolCall] | None:
    """
    Try to parse tool calls from the model's response text.

    Uses robust brace-matching extraction (handles nested JSON, arrays, etc.)
    then validates tool names against the provided tool definitions.
    Returns None if no valid tool calls are found.
    """
    json_str = _extract_json_object(response_text, "tool_calls")
    if not json_str:
        return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        log.debug(f"Failed to parse tool call JSON: {json_str[:200]}")
        return None

    if "tool_calls" not in parsed or not isinstance(parsed["tool_calls"], list):
        return None

    # Validate that the called functions are in the provided tools
    valid_names = {t.function.name for t in tools}
    result: list[ToolCall] = []

    for call in parsed["tool_calls"]:
        name = call.get("name", "")
        if name not in valid_names:
            log.warning(f"Model called unknown tool: {name}")
            continue

        arguments = call.get("arguments", {})
        if isinstance(arguments, dict):
            arguments_str = json.dumps(arguments)
        else:
            arguments_str = str(arguments)

        result.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                type="function",
                function=FunctionCallInfo(name=name, arguments=arguments_str),
            )
        )

    return result if result else None


# ── Routes ──────────────────────────────────────────────────────


@openai_router.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List available models — returns our single browser-backed model."""
    model_id = _get_model_id()
    owned_by = "anthropic" if Config.PROVIDER == "claude" else "catgpt"
    return ModelListResponse(
        data=[
            ModelObject(id=model_id, owned_by=owned_by),
        ]
    )


@openai_router.post("/v1/images/generations", response_model=ImagesResponse)
async def create_image(
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """
    OpenAI-compatible image generation endpoint.

    Sends the prompt to ChatGPT which uses DALL-E to generate images.
    Downloads the generated images and returns them in OpenAI format.
    Supports response_format='b64_json' (default) or 'url' (local file path).
    """
    import base64

    if _database is not None:
        authorize_capability(
            http_request,
            _database,
            "image.generate",
            session_id=request.session_id,
            agent_id=http_request.headers.get("x-agent-id"),
        )

    if not request.prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    # Claude does not support image generation
    if Config.PROVIDER == "claude":
        raise HTTPException(
            status_code=501,
            detail="Image generation is not supported by Claude. This feature is only available with the ChatGPT provider.",
        )

    if request.session_id:
        return await _image_via_session_pool(request, http_request)

    client = _get_client()

    async with _get_lock():
        start_time = time.time()

        # Build an image-generation prompt.
        # n > 1: we ask ChatGPT to generate multiple images
        # size/quality/style hints are included but ChatGPT web may ignore them.
        prompt_parts = [f"Generate an image: {request.prompt}"]
        if request.n and request.n > 1:
            prompt_parts.append(f"Please generate {request.n} different images.")
        if request.size and request.size != "1024x1024":
            prompt_parts.append(f"Image size: {request.size}.")
        if request.quality == "hd":
            prompt_parts.append("Make it high-definition / highly detailed.")
        if request.style == "natural":
            prompt_parts.append("Use a natural, realistic style.")

        full_prompt = " ".join(prompt_parts)

        log.info(
            f"POST /v1/images/generations — prompt_chars={len(request.prompt)}, "
            f"n={request.n}, size={request.size}, response_format={request.response_format}"
        )

        # Start a fresh conversation to avoid thread exhaustion
        await _ensure_fresh_chat()

        # Send to ChatGPT
        try:
            result = await client.send_message(full_prompt)
        except ProviderError as error:
            raise provider_error(error, session_id=request.session_id) from error
        except Exception as error:
            log.error("Provider image request failed", exc_info=True)
            raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider image request failed", retryable=True) from error

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Check if ChatGPT generated images
        if not result.images:
            # ChatGPT may have responded with text instead of generating an image.
            # This can happen when the model declines or gives a text description.
            log.warning("No images detected in response (elapsed_ms=%s, chars=%s)", elapsed_ms, len(result.message or ""))
            raise HTTPException(
                status_code=422,
                detail="ChatGPT did not generate an image",
            )

        # Build image data objects
        image_data_list: list[ImageData] = []
        for img_info in result.images:
            revised_prompt = img_info.prompt_title or img_info.alt or request.prompt

            if request.response_format == "b64_json":
                # Read the downloaded file and base64-encode it
                if img_info.local_path:
                    try:
                        with open(img_info.local_path, "rb") as f:
                            img_bytes = f.read()
                        b64 = base64.b64encode(img_bytes).decode("utf-8")
                        image_data_list.append(
                            ImageData(
                                b64_json=b64,
                                revised_prompt=revised_prompt,
                            )
                        )
                    except Exception as e:
                        log.error(f"Failed to read image file {img_info.local_path}: {e}")
                else:
                    log.warning(f"Image has no local_path: {img_info.url[:80]}")
            else:
                # response_format == "url" → return local file path as URL
                image_data_list.append(
                    ImageData(
                        url=img_info.local_path or img_info.url,
                        revised_prompt=revised_prompt,
                    )
                )

        if not image_data_list:
            raise HTTPException(
                status_code=500,
                detail="Images were detected but could not be processed.",
            )

        log.info(
            f"Image generation complete: {len(image_data_list)} image(s), "
            f"{elapsed_ms}ms, format={request.response_format}"
        )

        _increment_thread_count()
        return ImagesResponse(data=image_data_list)


async def _image_via_session_pool(
    request: ImageGenerationRequest,
    http_request: Request,
) -> ImagesResponse:
    """Generate an image inside a project-owned persistent session."""
    owner = http_request.scope.get("catgpt.owner_id", "default")
    database = _database
    pool = _session_pool
    session = database.get_session(request.session_id, owner)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    prompt_parts = [f"Generate an image: {request.prompt}"]
    if request.n and request.n > 1:
        prompt_parts.append(f"Please generate {request.n} different images.")
    if request.size and request.size != "1024x1024":
        prompt_parts.append(f"Image size: {request.size}.")
    if request.quality == "hd":
        prompt_parts.append("Make it high-definition / highly detailed.")
    if request.style == "natural":
        prompt_parts.append("Use a natural, realistic style.")
    full_prompt = " ".join(prompt_parts)
    generated_dir = (
        Config.FILES_DIR / safe_owner_segment(owner) / request.session_id / "generated"
    )

    database.set_session_status(request.session_id, owner, "busy")
    database.add_message(request.session_id, "user", full_prompt)
    try:
        result, _ = await pool.send(
            session_id=request.session_id,
            provider_thread_id=session.get("provider_thread_id"),
            provider_thread_url=session.get("provider_thread_url"),
            message=full_prompt,
            file_paths=[],
            generated_dir=generated_dir,
            task_id=session.get("task_id"),
        )
        if result.thread_id:
            database.activate_session(
                request.session_id, owner, result.thread_id, result.thread_url or None
            )
        else:
            database.set_session_status(request.session_id, owner, "active")
    except ProviderError as error:
        database.set_session_status(request.session_id, owner, "error")
        raise provider_error(error, session_id=request.session_id) from error
    except Exception as error:
        database.set_session_status(request.session_id, owner, "error")
        raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider image request failed", retryable=True, session_id=request.session_id) from error

    if not result.images:
        database.add_message(request.session_id, "assistant", result.message or "")
        raise api_error(
            422,
            "image_not_generated",
            "IMAGE_NOT_GENERATED",
            "The provider did not generate an image",
            session_id=request.session_id,
        )

    import base64
    image_data_list: list[ImageData] = []
    if _file_service is None:
        raise HTTPException(status_code=503, detail="File service not initialized")
    for image in result.images:
        revised_prompt = image.prompt_title or image.alt or request.prompt
        if not image.local_path or not Path(image.local_path).is_file():
            continue
        source = Path(image.local_path)
        try:
            record = _file_service.register_generated(
                source,
                owner_id=owner,
                session_id=request.session_id,
                original_name=source.name,
            )
        except FileServiceError as error:
            log.warning("Generated image could not be registered: %s", error)
            continue
        content = Path(record["stored_path"]).read_bytes()
        if request.response_format == "b64_json":
            encoded = base64.b64encode(content).decode("utf-8")
            image_data_list.append(ImageData(b64_json=encoded, revised_prompt=revised_prompt))
        else:
            image_data_list.append(
                ImageData(
                    url=_file_payload(record, http_request)["download_url"],
                    revised_prompt=revised_prompt,
                )
            )
    if not image_data_list:
        raise HTTPException(status_code=500, detail="Generated images could not be processed")
    database.add_message(request.session_id, "assistant", result.message or "[image generated]")
    return ImagesResponse(data=image_data_list, session_id=request.session_id)


async def _complete_via_session_pool(
    request: ChatCompletionRequest, http_request: Request
) -> ChatCompletionResponse:
    """Execute an OpenAI-style completion through the managed session pool.

    Gives OpenAI SDK callers the same benefits as /v1/sessions messages:
    per-token user isolation, SQLite persistence and the globally serialized
    browser worker. Callers opt in via session_id or OPENAI_USE_SESSION_POOL.
    """
    start_time = time.time()
    owner = http_request.scope.get("catgpt.owner_id", "default")
    database = _database
    pool = _session_pool

    # ── Resolve or create the backing session ────────────────
    if request.session_id:
        session = database.get_session(request.session_id, owner)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session = database.create_session(
            owner_id=owner,
            title="OpenAI 兼容临时会话",
            provider=Config.PROVIDER,
        )
    session_id = session["id"]

    # ── Build the prompt (same tool injection as legacy path) ─
    messages = list(request.messages)
    has_tool_prompt = False
    if request.tools and request.tool_choice != "none":
        tool_system = _build_tool_system_prompt(
            request.tools, tool_choice=request.tool_choice
        )
        messages.insert(0, ChatMessage(role="system", content=tool_system))
        has_tool_prompt = True
    prompt = _build_prompt(messages)

    # ── Extract attachments from messages ────────────────────
    attachment_paths: list[str] = []
    for msg in request.messages:
        if msg.role == "user" and isinstance(msg.content, list):
            for url in _extract_image_urls(msg.content):
                attachment_paths.append(
                    await _download_file(
                        url,
                        owner_id=owner,
                        session_id=session_id,
                        require_session_match=True,
                    )
                )
            for fa in _extract_file_attachments(msg.content):
                attachment_paths.append(
                    await _download_file(
                        fa,
                        owner_id=owner,
                        session_id=session_id,
                        require_session_match=True,
                    )
                )

    # ── Send through the worker pool ─────────────────────────
    database.set_session_status(session_id, owner, "busy")
    database.add_message(session_id, "user", prompt)
    generated_dir = (
        Config.FILES_DIR / safe_owner_segment(owner) / session_id / "generated"
    )
    try:
        result, generated_paths = await pool.send(
            session_id=session_id,
            provider_thread_id=session.get("provider_thread_id"),
            provider_thread_url=session.get("provider_thread_url"),
            message=prompt,
            file_paths=attachment_paths,
            generated_dir=generated_dir,
            task_id=session.get("task_id"),
        )
        if result.thread_id:
            database.activate_session(
                session_id, owner, result.thread_id, result.thread_url or None
            )
        else:
            database.set_session_status(session_id, owner, "active")
    except HTTPException:
        database.set_session_status(session_id, owner, "error")
        raise
    except ProviderError as error:
        database.set_session_status(session_id, owner, "error")
        raise provider_error(error, session_id=session_id) from error
    except Exception as e:
        database.set_session_status(session_id, owner, "error")
        log.error("Provider error (session pool)", exc_info=True)
        raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider request failed", retryable=True, session_id=session_id) from e

    response_text = result.message or ""

    # ── Record captured generated files (best-effort, quota-aware) ─
    managed_links: list[str] = []
    seen_paths: set[str] = set()
    if _file_service is None:
        raise HTTPException(status_code=503, detail="File service not initialized")
    for image in result.images or []:
        if image.local_path and Path(image.local_path).is_file():
            generated_paths.append(Path(image.local_path))
    for path in generated_paths:
        path = Path(path).resolve()
        if str(path) in seen_paths or not path.is_file():
            continue
        seen_paths.add(str(path))
        try:
            record = _file_service.register_generated(
                path,
                owner_id=owner,
                session_id=session_id,
                original_name=path.name,
            )
        except FileServiceError as error:
            log.warning("Skipping generated file %s: %s", path.name, error)
            continue
        managed_links.append(_file_payload(record, http_request)["download_url"])
    if managed_links:
        links = "\n".join(f"[下载文件]({url})" for url in managed_links)
        response_text = f"{response_text}\n\n{links}".strip()

    database.add_message(session_id, "assistant", response_text)

    # ── Parse tool calls (same contract as legacy path) ──────
    tool_calls = None
    finish_reason = "stop"
    if has_tool_prompt and request.tools:
        tool_calls = _parse_tool_calls(response_text, request.tools)
        if tool_calls:
            finish_reason = "tool_calls"
            response_text = None

    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(response_text or "")
    elapsed_ms = int((time.time() - start_time) * 1000)
    log.info(
        f"Session-pool completion: session={session_id}, {elapsed_ms}ms, "
        f"finish_reason={finish_reason}"
    )

    return ChatCompletionResponse(
        model=request.model,
        choices=[
            Choice(
                index=0,
                message=ChoiceMessage(
                    role="assistant",
                    content=response_text,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        session_id=session_id,
    )


@openai_router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest,
    http_request: Request,
) -> ChatCompletionResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Converts the message array into a single prompt, sends it to ChatGPT
    via browser automation, and returns an OpenAI-formatted response.
    Supports tool/function calling via prompt injection.

    扩展：请求体中传入 `session_id`（POST /v1/sessions 创建的会话）时，
    本次请求改走多会话 worker 池执行，获得用户隔离、消息持久化与
    单 Worker 全局串行执行；响应体会回显实际使用的 session_id。
    """
    if _database is not None:
        authorize_capability(
            http_request,
            _database,
            "chat.openai_compatible",
            session_id=request.session_id,
            agent_id=http_request.headers.get("x-agent-id"),
        )
    # ── Optional managed-session routing ─────────────────────
    if request.session_id or Config.OPENAI_USE_SESSION_POOL:
        if request.stream:
            raise HTTPException(
                status_code=400,
                detail="Streaming is not supported. Set stream=false or omit it.",
            )
        if _database is None or _session_pool is None:
            raise HTTPException(
                status_code=503,
                detail="Session pool not initialized — cannot serve session-based requests",
            )
        return await _complete_via_session_pool(request, http_request)

    # ── Validate ────────────────────────────────────────────
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported. Set stream=false or omit it.",
        )

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    client = _get_client()

    async with _get_lock():
        start_time = time.time()

        # ── Build the prompt ────────────────────────────────
        messages = list(request.messages)

        # If tools are provided, inject tool definitions as a system prompt
        # (unless tool_choice="none", which means ignore tools)
        has_tool_prompt = False
        if request.tools and request.tool_choice != "none":
            tool_system = _build_tool_system_prompt(
                request.tools, tool_choice=request.tool_choice
            )
            # Prepend as the first system message
            messages.insert(0, ChatMessage(role="system", content=tool_system))
            has_tool_prompt = True

        prompt = _build_prompt(messages)
        log.info(
            f"POST /v1/chat/completions — model={request.model}, "
            f"{len(request.messages)} messages, prompt={len(prompt)} chars"
        )

        # ── Extract attachments from messages ──────────────
        image_paths: list[str] = []
        file_paths: list[str] = []
        for msg in request.messages:
            if msg.role == "user" and isinstance(msg.content, list):
                # Images (OpenAI vision format)
                image_urls = _extract_image_urls(msg.content)
                for url in image_urls:
                    image_paths.append(
                        await _download_file(
                            url,
                            owner_id=http_request.scope.get("catgpt.owner_id", "default"),
                            session_id=None,
                            require_session_match=False,
                        )
                    )
                # Generic file attachments
                file_attachments = _extract_file_attachments(msg.content)
                for fa in file_attachments:
                    file_paths.append(
                        await _download_file(
                            fa,
                            owner_id=http_request.scope.get("catgpt.owner_id", "default"),
                            session_id=None,
                            require_session_match=False,
                        )
                    )

        all_attachment_paths = image_paths + file_paths
        if all_attachment_paths:
            log.info(f"Extracted {len(image_paths)} image(s) and {len(file_paths)} file(s) from request")

        # Start a fresh conversation to avoid thread exhaustion
        await _ensure_fresh_chat()

        # ── Send to ChatGPT ────────────────────────────────
        try:
            result = await client.send_message(
                prompt,
                image_paths=image_paths or None,
                file_paths=file_paths or None,
            )
        except ProviderError as error:
            raise provider_error(error, session_id=request.session_id) from error
        except Exception as error:
            log.error("Provider request failed", exc_info=True)
            raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider request failed", retryable=True) from error

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        # ── Detect echo (extraction grabbed sent prompt instead of reply) ──
        _echo_markers = ["[System instruction:", "tool-calling mode", "Available functions:"]
        if response_text and has_tool_prompt and any(m in response_text for m in _echo_markers):
            log.warning("Response appears to echo the sent prompt — retrying extraction")
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.claude.detector import extract_last_response_via_copy
                else:
                    from src.chatgpt.detector import extract_last_response_via_copy
                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(m in retry_text for m in _echo_markers):
                    response_text = retry_text
                    log.info(f"Retry extraction succeeded: {len(response_text)} chars")
                else:
                    log.warning("Retry extraction still echoed — stripping system prefix")
                    # Last resort: try to find assistant content after the prompt
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        finish_reason = "stop"

        if has_tool_prompt and request.tools:
            tool_calls = _parse_tool_calls(response_text, request.tools)
            if tool_calls:
                finish_reason = "tool_calls"
                # When the model calls tools, content should be null
                response_text = None

        # ── Build response ──────────────────────────────────
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text or "")

        response = ChatCompletionResponse(
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(
                        role="assistant",
                        content=response_text,
                        tool_calls=tool_calls,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

        log.info(
            f"Response: {elapsed_ms}ms, finish_reason={finish_reason}, "
            f"tokens≈{response.usage.total_tokens}"
        )

        _increment_thread_count()
        return response


# ── Responses API (/v1/responses) ───────────────────────────────


def _responses_input_to_messages(
    input_data: str | list,
    instructions: str | None = None,
) -> list[ChatMessage]:
    """
    Convert Responses API `input` (string or item array) into a list of
    ChatMessage objects compatible with our existing _build_prompt().

    Handles:
      - Plain string → single user message
      - Array of message objects (role + content)
      - function_call items (assistant requested a tool)
      - function_call_output items (tool results)
    """
    messages: list[ChatMessage] = []

    # System prompt from `instructions`
    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    # Simple string input
    if isinstance(input_data, str):
        messages.append(ChatMessage(role="user", content=input_data))
        return messages

    # Array of items
    for item in input_data:
        if isinstance(item, str):
            messages.append(ChatMessage(role="user", content=item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role")

        if item_type == "function_call":
            # Assistant called a tool — record as assistant message with tool_calls
            name = item.get("name", "")
            arguments = item.get("arguments", "{}")
            call_id = item.get("call_id", f"call_{uuid.uuid4().hex[:24]}")
            messages.append(
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id=call_id,
                            type="function",
                            function=FunctionCallInfo(
                                name=name, arguments=arguments
                            ),
                        )
                    ],
                )
            )
        elif item_type == "function_call_output":
            # Tool result — map to role=tool
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            messages.append(
                ChatMessage(
                    role="tool",
                    content=output,
                    tool_call_id=call_id,
                )
            )
        elif item_type == "message" or role:
            # Regular message item
            r = role or item.get("role", "user")
            # Map "developer" role to "system"
            if r == "developer":
                r = "system"
            content = item.get("content", "")
            # Content can be a list of content parts or a string
            if isinstance(content, list):
                # Extract text from content parts
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "input_text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts) if text_parts else ""
            messages.append(ChatMessage(role=r, content=content))

    return messages


def _responses_tools_to_chat_tools(
    tools: list[dict],
) -> list[ToolDefinition]:
    """
    Convert flat Responses API tool definitions to nested Chat Completions
    ToolDefinition format so we can reuse _build_tool_system_prompt().

    Responses:  {"type": "function", "name": "X", "parameters": {...}}
    Chat:       {"type": "function", "function": {"name": "X", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            tool = _model_dump_compat(tool)
        if tool.get("type") != "function":
            continue
        result.append(
            ToolDefinition(
                type="function",
                function=FunctionDefinition(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
            )
        )
    return result


def _build_response_object(
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
    request: "ResponsesRequest",
    prompt_tokens: int,
    completion_tokens: int,
) -> ResponseObject:
    """Build a full ResponseObject from the model output."""
    now = int(time.time())
    output: list = []
    output_text_val: str | None = None

    if tool_calls:
        for tc in tool_calls:
            output.append(
                _model_dump_compat(
                    ResponseFunctionCall(
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                        call_id=tc.id,
                    )
                )
            )
    else:
        text = response_text or ""
        msg = ResponseOutputMessage(
            content=[ResponseOutputText(text=text)]
        )
        output.append(_model_dump_compat(msg))
        output_text_val = text

    usage = ResponseUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    # Reconstruct tools for the response envelope
    tools_echo = []
    if request.tools:
        for t in request.tools:
            tools_echo.append(
                _model_dump_compat(t)
            )

    return ResponseObject(
        created_at=now,
        completed_at=now,
        status="completed",
        model=request.model,
        instructions=request.instructions,
        max_output_tokens=request.max_output_tokens,
        output=output,
        output_text=output_text_val,
        temperature=request.temperature,
        top_p=request.top_p,
        tool_choice=request.tool_choice or "auto",
        tools=tools_echo,
        previous_response_id=request.previous_response_id,
        usage=usage,
        metadata=request.metadata or {},
        session_id=request.session_id,
    )


async def _stream_response_events(
    resp: ResponseObject,
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
):
    """
    Yield SSE events for a streaming Responses API call.

    Since the browser backend doesn't truly stream, we emit the full
    response as a burst of events matching the OpenAI SSE contract:
      response.created → response.in_progress →
      output_item.added → content_part.added →
      output_text.delta (full text as one chunk) →
      output_text.done → content_part.done →
      output_item.done → response.completed
    """
    seq = 0
    resp_dict = _model_dump_compat(resp)

    def _event(event_type: str, data: dict) -> str:
        data["type"] = event_type
        data["sequence_number"] = seq
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    # 1) response.created
    created_resp = dict(resp_dict)
    created_resp["status"] = "in_progress"
    created_resp["completed_at"] = None
    created_resp["output"] = []
    created_resp["output_text"] = None
    created_resp["usage"] = None
    yield _event("response.created", {"response": created_resp})
    seq += 1

    # 2) response.in_progress
    yield _event("response.in_progress", {"response": created_resp})
    seq += 1

    if tool_calls:
        # Emit function call output items
        for idx, tc in enumerate(tool_calls):
            fc_item = ResponseFunctionCall(
                name=tc.function.name,
                arguments=tc.function.arguments,
                call_id=tc.id,
            )
            fc_item = _model_dump_compat(fc_item)

            # output_item.added
            fc_added = dict(fc_item)
            fc_added["status"] = "in_progress"
            yield _event("response.output_item.added", {
                "output_index": idx,
                "item": fc_added,
            })
            seq += 1

            # function_call_arguments.delta (one burst)
            yield _event("response.function_call_arguments.delta", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "delta": tc.function.arguments,
            })
            seq += 1

            # function_call_arguments.done
            yield _event("response.function_call_arguments.done", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
            seq += 1

            # output_item.done
            yield _event("response.output_item.done", {
                "output_index": idx,
                "item": fc_item,
            })
            seq += 1
    else:
        # Emit text message output
        text = response_text or ""
        msg = ResponseOutputMessage(
            content=[ResponseOutputText(text=text)]
        )
        msg_dict = _model_dump_compat(msg)

        # output_item.added (empty content)
        msg_added = dict(msg_dict)
        msg_added["status"] = "in_progress"
        msg_added["content"] = []
        yield _event("response.output_item.added", {
            "output_index": 0,
            "item": msg_added,
        })
        seq += 1

        # content_part.added
        yield _event("response.content_part.added", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })
        seq += 1

        # output_text.delta — full text as one chunk
        if text:
            yield _event("response.output_text.delta", {
                "item_id": msg_dict["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            })
            seq += 1

        # output_text.done
        yield _event("response.output_text.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        })
        seq += 1

        # content_part.done
        yield _event("response.content_part.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        seq += 1

        # output_item.done
        yield _event("response.output_item.done", {
            "output_index": 0,
            "item": msg_dict,
        })
        seq += 1

    # response.completed
    yield _event("response.completed", {"response": resp_dict})


@openai_router.post("/v1/responses")
async def create_response(request: ResponsesRequest, http_request: Request):
    """
    OpenAI Responses API endpoint — compatible with Codex CLI.

    Accepts the Responses API format (flat tools, `input` field, `instructions`),
    translates to our internal format, sends to the browser, and returns a
    Responses-API-shaped response (or SSE stream).
    """
    if _database is not None:
        authorize_capability(
            http_request,
            _database,
            "response.create",
            session_id=request.session_id,
            agent_id=http_request.headers.get("x-agent-id"),
        )
    # ── Validate ────────────────────────────────────────────
    if not request.input:
        raise HTTPException(status_code=400, detail="input cannot be empty")

    if request.session_id:
        if request.stream:
            raise HTTPException(
                status_code=400,
                detail="Streaming is not supported with managed session_id yet",
            )
        messages = _responses_input_to_messages(
            request.input, instructions=request.instructions
        )
        chat_tools: list[ToolDefinition] | None = None
        if request.tools:
            raw_tools = [
                _model_dump_compat(tool)
                for tool in request.tools
            ]
            chat_tools = _responses_tools_to_chat_tools(raw_tools)
        completion = await _complete_via_session_pool(
            ChatCompletionRequest(
                model=request.model,
                messages=messages,
                tools=chat_tools,
                tool_choice=request.tool_choice,
                temperature=request.temperature,
                max_tokens=request.max_output_tokens,
                top_p=request.top_p,
                session_id=request.session_id,
            ),
            http_request,
        )
        choice = completion.choices[0]
        response_text = choice.message.content
        response_object = _build_response_object(
            response_text,
            choice.message.tool_calls,
            request,
            completion.usage.prompt_tokens,
            completion.usage.completion_tokens,
        )
        return _model_dump_compat(response_object)

    client = _get_client()

    async with _get_lock():
        start_time = time.time()

        # ── Convert input to ChatMessage list ───────────────
        messages = _responses_input_to_messages(
            request.input, instructions=request.instructions
        )

        # ── Convert flat tools to nested format ─────────────
        chat_tools: list[ToolDefinition] | None = None
        has_tool_prompt = False
        if request.tools:
            raw_tools = [
                _model_dump_compat(t)
                for t in request.tools
            ]
            chat_tools = _responses_tools_to_chat_tools(raw_tools)
            if chat_tools and request.tool_choice != "none":
                tool_system = _build_tool_system_prompt(
                    chat_tools, tool_choice=request.tool_choice
                )
                messages.insert(
                    0, ChatMessage(role="system", content=tool_system)
                )
                has_tool_prompt = True

        prompt = _build_prompt(messages)
        log.info(
            f"POST /v1/responses — model={request.model}, "
            f"input_type={'string' if isinstance(request.input, str) else 'array'}, "
            f"prompt={len(prompt)} chars, stream={request.stream}"
        )

        # Start a fresh conversation to avoid thread exhaustion
        await _ensure_fresh_chat()

        # ── Send to browser ────────────────────────────────
        try:
            result = await client.send_message(prompt)
        except ProviderError as error:
            raise provider_error(error, session_id=request.session_id) from error
        except Exception as error:
            try:
                page_closed = client.page.is_closed()
            except Exception:
                page_closed = True
            if page_closed:
                unknown = ProviderStateUnknownError(
                    "Browser closed after the request began; the prompt will not be sent again automatically"
                )
                raise provider_error(unknown, session_id=request.session_id) from error
            log.error("Provider Responses request failed", exc_info=True)
            raise api_error(502, "provider_error", "PROVIDER_ERROR", "Provider request failed", retryable=True) from error

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        # ── Detect echo ────────────────────────────────────
        _echo_markers = [
            "[System instruction:",
            "tool-calling mode",
            "Available functions:",
        ]
        if (
            response_text
            and has_tool_prompt
            and any(m in response_text for m in _echo_markers)
        ):
            log.warning(
                "Response appears to echo the sent prompt — retrying extraction"
            )
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.claude.detector import extract_last_response_via_copy
                else:
                    from src.chatgpt.detector import extract_last_response_via_copy

                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(
                    m in retry_text for m in _echo_markers
                ):
                    response_text = retry_text
                    log.info(
                        f"Retry extraction succeeded: {len(response_text)} chars"
                    )
                else:
                    log.warning(
                        "Retry extraction still echoed — stripping system prefix"
                    )
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        if has_tool_prompt and chat_tools:
            tool_calls = _parse_tool_calls(response_text, chat_tools)
            if tool_calls:
                response_text = None

        # ── Build response ──────────────────────────────────
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text or "")

        resp = _build_response_object(
            response_text, tool_calls, request,
            prompt_tokens, completion_tokens,
        )

        log.info(
            f"Response: {elapsed_ms}ms, "
            f"tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"tokens≈{resp.usage.total_tokens if resp.usage else 0}"
        )

        _increment_thread_count()

        # ── Stream or return ────────────────────────────────
        if request.stream:
            return StreamingResponse(
                _stream_response_events(resp, response_text, tool_calls),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return _model_dump_compat(resp)
