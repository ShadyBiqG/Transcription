from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class SecretProtectionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def protect_secret(secret: str) -> bytes:
    if os.name != "nt":
        raise SecretProtectionError("Защищенное хранение ключа поддерживается только в Windows")
    source, source_buffer = _blob(secret.encode("utf-8"))
    result = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "RouterAI API key",
        None,
        None,
        None,
        0x01,
        ctypes.byref(result),
    ):
        raise SecretProtectionError("Windows не смогла защитить API-ключ")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)
        del source_buffer


def unprotect_secret(ciphertext: bytes) -> str:
    if os.name != "nt":
        raise SecretProtectionError("Защищенное хранение ключа поддерживается только в Windows")
    source, source_buffer = _blob(ciphertext)
    result = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(result)
    ):
        raise SecretProtectionError("API-ключ недоступен текущей учетной записи Windows")
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)
        del source_buffer


def mask_secret(secret: str) -> str:
    stripped = secret.strip()
    if len(stripped) <= 8:
        return "•" * len(stripped)
    return f"{stripped[:4]}…{stripped[-4:]}"
