"""Cross-platform project-token encryption (DPAPI or configured Fernet key)."""

from __future__ import annotations

import base64
import os
import sys

_ENTROPY = b"GPT-FastAPI/project-token/v1"


def _fernet():
    key = os.getenv("TOKEN_VAULT_KEY", "").strip().encode("ascii")
    if not key:
        raise RuntimeError("TOKEN_VAULT_KEY is required for the token vault on this platform")
    try:
        from cryptography.fernet import Fernet

        return Fernet(key)
    except (ImportError, ValueError) as error:
        raise RuntimeError("TOKEN_VAULT_KEY must be a valid Fernet key") from error


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
    _FLAGS = 0x01 | 0x04

    def _blob(payload: bytes):
        buffer = ctypes.create_string_buffer(payload)
        return _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    def _copy_and_free(blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            if blob.pbData:
                _kernel32.LocalFree(ctypes.cast(blob.pbData, wintypes.HLOCAL))

    def _dpapi_protect(token: str) -> str:
        input_blob, input_buffer = _blob(token.encode("utf-8"))
        entropy_blob, entropy_buffer = _blob(_ENTROPY)
        output_blob = _DataBlob()
        success = _crypt32.CryptProtectData(
            ctypes.byref(input_blob), "GPT-FastAPI project token",
            ctypes.byref(entropy_blob), None, None, _FLAGS, ctypes.byref(output_blob),
        )
        _ = (input_buffer, entropy_buffer)
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        return base64.b64encode(_copy_and_free(output_blob)).decode("ascii")

    def _dpapi_unprotect(ciphertext: str) -> str:
        encrypted = base64.b64decode(ciphertext, validate=True)
        input_blob, input_buffer = _blob(encrypted)
        entropy_blob, entropy_buffer = _blob(_ENTROPY)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        success = _crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), ctypes.byref(description), ctypes.byref(entropy_blob),
            None, None, _FLAGS, ctypes.byref(output_blob),
        )
        _ = (input_buffer, entropy_buffer)
        try:
            if not success:
                raise ctypes.WinError(ctypes.get_last_error())
            return _copy_and_free(output_blob).decode("utf-8")
        finally:
            if description:
                _kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))


def protect_token(token: str) -> str:
    if sys.platform == "win32":
        return "dpapi:" + _dpapi_protect(token)
    return "fernet:" + _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def unprotect_token(ciphertext: str) -> str:
    if ciphertext.startswith("fernet:"):
        return _fernet().decrypt(ciphertext[7:].encode("ascii")).decode("utf-8")
    if ciphertext.startswith("dpapi:"):
        if sys.platform != "win32":
            raise RuntimeError("DPAPI ciphertext can only be decrypted on Windows")
        return _dpapi_unprotect(ciphertext[6:])
    # Backward compatibility for existing Windows rows created before prefixes.
    if sys.platform == "win32":
        return _dpapi_unprotect(ciphertext)
    raise RuntimeError("Unknown token-vault ciphertext format")
