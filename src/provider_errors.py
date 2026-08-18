"""Stable provider-facing exceptions that API routes can safely translate."""

from __future__ import annotations


class ProviderError(RuntimeError):
    code = "PROVIDER_ERROR"
    error_type = "provider_error"
    status_code = 502
    retryable = True

    def __init__(self, message: str, *, partial_output: str | None = None) -> None:
        super().__init__(message)
        self.partial_output = partial_output


class ProviderTimeoutError(ProviderError):
    code = "PROVIDER_TIMEOUT"
    error_type = "provider_timeout"
    status_code = 504


class AttachmentUploadError(ProviderError):
    code = "ATTACHMENT_UPLOAD_FAILED"
    error_type = "attachment_upload_failed"
    status_code = 422
    retryable = False

    def __init__(self, failed_files: list[str], message: str | None = None) -> None:
        self.failed_files = failed_files
        super().__init__(message or f"{len(failed_files)} attachment(s) failed to upload")


class ProviderStateUnknownError(ProviderError):
    code = "PROVIDER_STATE_UNKNOWN"
    error_type = "provider_state_unknown"
    status_code = 503
