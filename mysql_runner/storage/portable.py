"""Portable export/import of server profiles.

Profiles normally live encrypted under the local Data Encryption Key, which never
leaves the machine. To move connections to another PC we re-encrypt the selected
profiles under a user-supplied *passphrase* (PBKDF2-HMAC-SHA256 + Fernet) and write
a self-contained ``.mrx`` file. Importing on the other machine asks for the same
passphrase and merges the profiles into that machine's vault.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from mysql_runner.storage.models import ServerProfile

_MAGIC = "sitekeeper-export"
#: Exports written before the rename. Still imported, of course.
_LEGACY_MAGIC = "mysql-runner-export"
_VERSION = 1
_ITERATIONS = 480_000
_SALT_BYTES = 16


class PortableError(Exception):
    """Raised when an export bundle cannot be read or decrypted."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def export_profiles(
    profiles: list[ServerProfile], passphrase: str, path: str | Path
) -> None:
    """Encrypt ``profiles`` under ``passphrase`` and write them to ``path``."""
    if not passphrase:
        raise PortableError("A passphrase is required to export.")
    salt = os.urandom(_SALT_BYTES)
    key = _derive_key(passphrase, salt)
    payload = json.dumps([p.to_dict() for p in profiles]).encode("utf-8")
    token = Fernet(key).encrypt(payload)
    bundle = {
        "magic": _MAGIC,
        "version": _VERSION,
        "salt": base64.b64encode(salt).decode("ascii"),
        "data": base64.b64encode(token).decode("ascii"),
    }
    Path(path).write_text(json.dumps(bundle, indent=2), encoding="utf-8")


def import_profiles(path: str | Path, passphrase: str) -> list[ServerProfile]:
    """Decrypt and return the profiles stored in an export bundle."""
    try:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise PortableError("The file is not a valid export bundle.") from exc
    if bundle.get("magic") not in (_MAGIC, _LEGACY_MAGIC):
        raise PortableError("The file is not a Sitekeeper export.")
    try:
        salt = base64.b64decode(bundle["salt"])
        token = base64.b64decode(bundle["data"])
    except (KeyError, ValueError) as exc:
        raise PortableError("The export bundle is corrupted.") from exc
    key = _derive_key(passphrase, salt)
    try:
        payload = Fernet(key).decrypt(token)
    except InvalidToken as exc:
        raise PortableError("Incorrect passphrase or corrupted file.") from exc
    raw = json.loads(payload.decode("utf-8"))
    return [ServerProfile.from_dict(item) for item in raw]
