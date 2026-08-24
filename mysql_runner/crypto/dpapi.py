"""Windows DPAPI blob protection (ctypes, no third-party dependency).

Used by the vault's password-free modes: instead of deriving the key-encryption
key from a typed master password, the Data Encryption Key is sealed with
``CryptProtectData`` so only the current Windows user account can unseal it.
Credentials therefore stay encrypted at rest — copying ``vault.json`` and
``servers.enc`` to another account or machine yields nothing usable — while the
app never has to prompt.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# Extra entropy mixed into the blob so a DPAPI blob produced by another
# application cannot be swapped in for ours.
#
# This string is part of the crypto, not a label: it is required to unprotect a
# blob that was protected with it. It keeps the old application name for that
# reason alone - changing it would make every vault sealed before the rename
# undecryptable, with nothing to fall back on. The description below is only a
# caption Windows shows, so it follows the new name freely.
_ENTROPY = b"MySQLRunner/vault/v2"
_DESCRIPTION = "Sitekeeper vault key"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DPAPIError(Exception):
    """Raised when a DPAPI protect/unprotect call fails."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_available() -> bool:
    """Whether DPAPI can be used on this platform."""
    return sys.platform == "win32"


def _blob_in(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def _require_windows() -> None:
    if not is_available():
        raise DPAPIError("Windows data protection is only available on Windows.")


def protect(data: bytes) -> bytes:
    """Seal ``data`` to the current Windows user account."""
    _require_windows()
    data_in = _blob_in(data)
    entropy = _blob_in(_ENTROPY)
    data_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_in),
        ctypes.c_wchar_p(_DESCRIPTION),
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise DPAPIError(
            f"CryptProtectData failed (error {ctypes.GetLastError()})."
        )
    try:
        return _blob_bytes(data_out)
    finally:
        _free(data_out)


def unprotect(blob: bytes) -> bytes:
    """Unseal a blob produced by :func:`protect`.

    Raises :class:`DPAPIError` when the blob belongs to a different Windows
    account, was tampered with, or was sealed with different entropy.
    """
    _require_windows()
    data_in = _blob_in(blob)
    entropy = _blob_in(_ENTROPY)
    data_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(data_out),
    )
    if not ok:
        raise DPAPIError(
            "Could not unseal the vault key for this Windows account "
            f"(error {ctypes.GetLastError()})."
        )
    try:
        return _blob_bytes(data_out)
    finally:
        _free(data_out)
