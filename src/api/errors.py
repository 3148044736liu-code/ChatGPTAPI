"""Uniform, non-leaking error responses for the public API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.log import setup_logging
from src.provider_errors import AttachmentUploadError, ProviderError

log = setup_logging("api_errors")


def api_error(
    status_code: int,
    error_type: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    **context: Any,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "type": error_type,
            "code": code,
            "message": message,
            "retryable": retryable,
            **{key: value for key, value in context.items() if value is not None},
        },
    )


def provider_error(error: ProviderError, **context: Any) -> HTTPException:
    extra: dict[str, Any] = dict(context)
    if error.partial_output:
        extra["partial_output"] = error.partial_output
    if isinstance(error, AttachmentUploadError):
        extra["failed_files"] = error.failed_files
    return api_error(
        error.status_code,
        error.error_type,
        error.code,
        str(error),
        retryable=error.retryable,
        **extra,
    )


def _request_id(request: Request) -> str:
    existing = request.scope.get("catgpt.request_id") or request.headers.get("x-request-id")
    request_id = str(existing or f"req_{uuid.uuid4().hex}")
    request.scope["catgpt.request_id"] = request_id
    return request_id


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, error: HTTPException):
        request_id = _request_id(request)
        if isinstance(error.detail, dict):
            payload = dict(error.detail)
        else:
            payload = {
                "type": "request_error",
                "code": f"HTTP_{error.status_code}",
                "message": str(error.detail),
                "retryable": error.status_code in {429, 502, 503, 504},
            }
        payload.setdefault("request_id", request_id)
        payload.setdefault("retryable", False)
        return JSONResponse(status_code=error.status_code, content={"error": payload}, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "type": "validation_error",
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "retryable": False,
                    "request_id": _request_id(request),
                    "fields": error.errors(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, error: Exception):
        request_id = _request_id(request)
        log.error("Unhandled API error | request_id=%s | error=%s", request_id, error, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "code": "INTERNAL_ERROR",
                    "message": "The server could not complete the request",
                    "retryable": False,
                    "request_id": request_id,
                }
            },
        )
