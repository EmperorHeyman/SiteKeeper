"""Credential vault.

A random Data Encryption Key (DEK) protects all stored credentials. How that
DEK is itself protected depends on the vault's *protection mode*, recorded in a
small JSON metadata file on disk:

"password"
    The DEK is encrypted with a Key Encryption Key (KEK) derived from the user's
    master password via PBKDF2-HMAC-SHA256. For convenience the plaintext DEK is
    also cached in the OS keyring (Windows Credential Manager); on unlock we try
    the keyring first and fall back to prompting.

"windows"
    Password protection is turned off. The DEK is sealed with the Windows Data
    Protection API, tied to the current Windows user account, so the app never
    prompts - yet servers.enc stays encrypted at rest and the vault is useless
    to another account or on another machine.

Switching between modes re-seals the same DEK, so stored servers survive
intact.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from mysql_runner.crypto import dpapi
from mysql_runner.paths import vault_path

_KEYRING_SERVICE = "Sitekeeper"
#: The service name used before the rename. Read from once, then written back
#: under the new name, so nobody has to retype a master password over a rename.
_LEGACY_KEYRING_SERVICE = "MySQLRunner"
_KEYRING_USERNAME = "dek"
_PBKDF2_ITERATIONS = 480_000
_SALT_BYTES = 16

#: Protection modes (see the module docstring).
PROTECTION_PASSWORD = "password"
PROTECTION_WINDOWS = "windows"


#: Set SITEKEEPER_NO_KEYRING=1 to bypass the OS keyring entirely. The cache is
#: keyed by application name rather than by vault file, so anything sharing this
#: machine's credential store - a second install, a portable copy, a test run -
#: would otherwise overwrite the same entry. The old MYSQLRUNNER_NO_KEYRING is
#: still honoured; scripts that set it were protecting a real vault.
_NO_KEYRING_ENV = "SITEKEEPER_NO_KEYRING"
_LEGACY_NO_KEYRING_ENV = "MYSQLRUNNER_NO_KEYRING"


def keyring_enabled() -> bool:
    """Whether the OS keyring cache may be used at all."""
    return not any(
        os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}
        for name in (_NO_KEYRING_ENV, _LEGACY_NO_KEYRING_ENV)
    )


def _get_keyring_module():
    """Import keyring lazily so packaging can omit it when unavailable."""
    if not keyring_enabled():
        return None
    try:
        return importlib.import_module("keyring")
    except Exception:
        return None


class VaultError(Exception):
    """Base class for vault errors."""


class InvalidMasterPassword(VaultError):
    """Raised when the supplied master password cannot decrypt the DEK."""


class VaultNotInitialized(VaultError):
    """Raised when an operation needs an initialized vault but none exists."""


def _derive_kek(master_password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))


@dataclass
class Vault:
    """Holds the active Data Encryption Key for the running session."""

    _dek: bytes

    @property
    def fernet(self) -> Fernet:
        return Fernet(self._dek)

    def encrypt(self, data: bytes) -> bytes:
        return self.fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        return self.fernet.decrypt(token)

    def lock(self) -> None:
        """Wipe the in-memory key reference."""
        self._dek = b""


def is_initialized() -> bool:
    return vault_path().exists()


# ----- metadata -----------------------------------------------------------
def _write_metadata(
    encrypted_dek: bytes, protection: str, salt: bytes | None = None
) -> None:
    payload: dict[str, object] = {
        "version": 2,
        "protection": protection,
        "encrypted_dek": base64.b64encode(encrypted_dek).decode("ascii"),
    }
    if salt is not None:
        payload["salt"] = base64.b64encode(salt).decode("ascii")
    path = vault_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_metadata() -> tuple[str, bytes | None, bytes]:
    """Return (protection, salt, encrypted_dek) for the stored vault."""
    if not is_initialized():
        raise VaultNotInitialized("Vault has not been created yet.")
    try:
        data = json.loads(vault_path().read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise VaultError("The vault metadata file is unreadable.") from exc
    # Version 1 files predate protection modes and are always password-based.
    protection = str(data.get("protection", PROTECTION_PASSWORD))
    raw_salt = data.get("salt")
    salt = base64.b64decode(raw_salt) if raw_salt else None
    return protection, salt, base64.b64decode(data["encrypted_dek"])


def protection_mode() -> str:
    """Return the protection mode of the stored vault (password when absent)."""
    if not is_initialized():
        return PROTECTION_PASSWORD
    try:
        return _read_metadata()[0]
    except VaultError:
        return PROTECTION_PASSWORD


def requires_password() -> bool:
    """Whether unlocking this vault needs a master password."""
    return protection_mode() == PROTECTION_PASSWORD


# ----- keyring cache ------------------------------------------------------
def _cache_dek_in_keyring(dek: bytes) -> None:
    keyring = _get_keyring_module()
    if keyring is None:
        return
    try:
        keyring.set_password(
            _KEYRING_SERVICE,
            _KEYRING_USERNAME,
            base64.b64encode(dek).decode("ascii"),
        )
    except Exception:
        # Keyring is a convenience layer only; ignore backend failures.
        pass


def _load_dek_from_keyring() -> bytes | None:
    keyring = _get_keyring_module()
    if keyring is None:
        return None
    stored = _read_keyring(keyring, _KEYRING_SERVICE)
    adopted = False
    if not stored:
        # Pre-rename entry: use it, then write it back under the new name so
        # this only happens once.
        stored = _read_keyring(keyring, _LEGACY_KEYRING_SERVICE)
        adopted = bool(stored)
    if not stored:
        return None
    try:
        dek = base64.b64decode(stored)
    except Exception:
        return None
    if adopted:
        _cache_dek_in_keyring(dek)
    return dek


def _read_keyring(keyring, service: str) -> str | None:
    try:
        return keyring.get_password(service, _KEYRING_USERNAME)
    except Exception:
        return None


def clear_keyring_cache() -> None:
    keyring = _get_keyring_module()
    if keyring is None:
        return
    for service in (_KEYRING_SERVICE, _LEGACY_KEYRING_SERVICE):
        try:
            keyring.delete_password(service, _KEYRING_USERNAME)
        except Exception:
            pass


# ----- creation -----------------------------------------------------------
def initialize(master_password: str) -> Vault:
    """Create a brand-new vault protected by master_password."""
    salt = os.urandom(_SALT_BYTES)
    dek = Fernet.generate_key()
    kek = _derive_kek(master_password, salt)
    encrypted_dek = Fernet(kek).encrypt(dek)
    _write_metadata(encrypted_dek, PROTECTION_PASSWORD, salt)
    _cache_dek_in_keyring(dek)
    return Vault(_dek=dek)


def initialize_keyless() -> Vault:
    """Create a brand-new vault with no master password (Windows-sealed DEK)."""
    if not dpapi.is_available():
        raise VaultError(
            "Password-free mode needs Windows data protection, which is not "
            "available on this platform."
        )
    dek = Fernet.generate_key()
    try:
        sealed = dpapi.protect(dek)
    except dpapi.DPAPIError as exc:
        raise VaultError(str(exc)) from exc
    _write_metadata(sealed, PROTECTION_WINDOWS)
    return Vault(_dek=dek)


# ----- unlocking ----------------------------------------------------------
def unlock_keyless() -> Vault:
    """Unlock a vault whose DEK is sealed to the Windows account."""
    protection, _salt, sealed = _read_metadata()
    if protection != PROTECTION_WINDOWS:
        raise VaultError("This vault is protected by a master password.")
    try:
        dek = dpapi.unprotect(sealed)
    except dpapi.DPAPIError as exc:
        raise VaultError(str(exc)) from exc
    return Vault(_dek=dek)


def unlock_with_keyring() -> Vault | None:
    """Try to unlock using the DEK cached in the OS keyring."""
    if not is_initialized():
        return None
    dek = _load_dek_from_keyring()
    if dek is None:
        return None
    # Sanity-check that the cached DEK is usable.
    try:
        Fernet(dek)
    except Exception:
        return None
    return Vault(_dek=dek)


def unlock_with_password(master_password: str) -> Vault:
    """Unlock the vault using the master password."""
    protection, salt, encrypted_dek = _read_metadata()
    if protection != PROTECTION_PASSWORD or salt is None:
        raise VaultError("This vault does not use a master password.")
    kek = _derive_kek(master_password, salt)
    try:
        dek = Fernet(kek).decrypt(encrypted_dek)
    except InvalidToken as exc:
        raise InvalidMasterPassword("Incorrect master password.") from exc
    _cache_dek_in_keyring(dek)
    return Vault(_dek=dek)


# ----- switching protection ----------------------------------------------
def change_master_password(old_password: str, new_password: str) -> None:
    """Re-encrypt the DEK under a new master password."""
    protection, salt, encrypted_dek = _read_metadata()
    if protection != PROTECTION_PASSWORD or salt is None:
        raise VaultError(
            "Password protection is turned off. Turn it back on in Settings to "
            "choose a master password."
        )
    old_kek = _derive_kek(old_password, salt)
    try:
        dek = Fernet(old_kek).decrypt(encrypted_dek)
    except InvalidToken as exc:
        raise InvalidMasterPassword("Incorrect master password.") from exc
    new_salt = os.urandom(_SALT_BYTES)
    new_kek = _derive_kek(new_password, new_salt)
    new_encrypted = Fernet(new_kek).encrypt(dek)
    _write_metadata(new_encrypted, PROTECTION_PASSWORD, new_salt)
    _cache_dek_in_keyring(dek)


def disable_password(current_password: str) -> None:
    """Turn off password protection, re-sealing the DEK with Windows DPAPI.

    Stored servers are untouched: the same DEK is simply protected a different
    way. Raises InvalidMasterPassword if current_password is wrong.
    """
    if not dpapi.is_available():
        raise VaultError(
            "Password-free mode needs Windows data protection, which is not "
            "available on this platform."
        )
    protection, salt, encrypted_dek = _read_metadata()
    if protection == PROTECTION_WINDOWS:
        return  # Already off.
    if salt is None:
        raise VaultError("The vault metadata is incomplete.")
    kek = _derive_kek(current_password, salt)
    try:
        dek = Fernet(kek).decrypt(encrypted_dek)
    except InvalidToken as exc:
        raise InvalidMasterPassword("Incorrect master password.") from exc
    try:
        sealed = dpapi.protect(dek)
    except dpapi.DPAPIError as exc:
        raise VaultError(str(exc)) from exc
    _write_metadata(sealed, PROTECTION_WINDOWS)
    # The keyring copy is redundant once the DEK is sealed to the account, and
    # leaving it behind would keep a second copy of the key around.
    clear_keyring_cache()


def enable_password(new_password: str) -> None:
    """Turn password protection back on, re-keying the DEK under a password."""
    protection, _salt, sealed = _read_metadata()
    if protection == PROTECTION_PASSWORD:
        raise VaultError(
            "Password protection is already on. Use “Change master "
            "password…” to pick a different one."
        )
    try:
        dek = dpapi.unprotect(sealed)
    except dpapi.DPAPIError as exc:
        raise VaultError(str(exc)) from exc
    salt = os.urandom(_SALT_BYTES)
    kek = _derive_kek(new_password, salt)
    _write_metadata(Fernet(kek).encrypt(dek), PROTECTION_PASSWORD, salt)
    _cache_dek_in_keyring(dek)
