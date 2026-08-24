"""Process-wide vault and connection store.

The Qt app kept the open Vault in a closure inside app.run(); the backend needs
the same thing reachable from every request handler, so it lives here behind a
lock. Nothing is written to disk that the Qt build cannot also read - both front
ends share %APPDATA%\\Sitekeeper.
"""

from __future__ import annotations

import threading

from mysql_runner.crypto import vault as vault_mod
from mysql_runner.storage.settings import Settings as UiSettings
from mysql_runner.storage.store import ServerStore, StoreError, opens_store


class VaultLocked(RuntimeError):
    """Raised when an operation needs credentials but the vault is locked."""


class VaultState:
    """Holds the open vault and its store for the life of the process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vault: vault_mod.Vault | None = None
        self._store: ServerStore | None = None

    # ----- status ---------------------------------------------------------
    @property
    def is_initialized(self) -> bool:
        return vault_mod.is_initialized()

    @property
    def is_unlocked(self) -> bool:
        with self._lock:
            return self._store is not None

    @property
    def protection(self) -> str:
        return vault_mod.protection_mode()

    @property
    def requires_password(self) -> bool:
        return vault_mod.requires_password()

    def status(self) -> dict[str, object]:
        return {
            "initialized": self.is_initialized,
            "unlocked": self.is_unlocked,
            "protection": self.protection,
            "requires_password": self.requires_password,
        }

    # ----- opening --------------------------------------------------------
    def _adopt(self, vault: vault_mod.Vault) -> None:
        """Attach an opened vault, loading its store."""
        try:
            store = ServerStore(vault)
        except StoreError as exc:
            raise RuntimeError(str(exc)) from exc
        with self._lock:
            self._vault = vault
            self._store = store

    def try_auto_unlock(self) -> bool:
        """Open the vault without prompting, when that is possible.

        Password-free vaults unseal from the Windows account; password vaults
        fall back to the keyring cache the Qt app may have populated.

        This runs during application startup and is a convenience only, so it
        never raises: any failure just leaves the vault locked and the UI shows
        its unlock screen.
        """
        if not self.is_initialized or self.is_unlocked:
            return self.is_unlocked
        try:
            if not vault_mod.requires_password():
                self._adopt(vault_mod.unlock_keyless())
                return True
            cached = vault_mod.unlock_with_keyring()
            if cached is None:
                return False
            # A cached key that cannot decrypt the store is stale; discard it
            # so the next launch asks for the password instead of retrying it.
            if not opens_store(cached):
                vault_mod.clear_keyring_cache()
                return False
            self._adopt(cached)
            return True
        except (vault_mod.VaultError, RuntimeError, OSError):
            return False

    def unlock_with_password(self, password: str) -> None:
        self._adopt(vault_mod.unlock_with_password(password))

    def create(self, password: str | None) -> None:
        """First-run: create a vault, with or without a master password."""
        if password:
            self._adopt(vault_mod.initialize(password))
        else:
            self._adopt(vault_mod.initialize_keyless())

    def lock(self) -> None:
        with self._lock:
            if self._vault is not None:
                self._vault.lock()
            self._vault = None
            self._store = None

    # ----- access ---------------------------------------------------------
    def store(self) -> ServerStore:
        with self._lock:
            if self._store is None:
                raise VaultLocked("The vault is locked.")
            return self._store

    def vault(self) -> vault_mod.Vault:
        with self._lock:
            if self._vault is None:
                raise VaultLocked("The vault is locked.")
            return self._vault

    def reload_store(self) -> None:
        """Re-read servers.enc, e.g. after the Qt app wrote to it."""
        with self._lock:
            if self._store is not None:
                self._store.load()


state = VaultState()


def ui_settings() -> UiSettings:
    """Load the shared UI preferences file (same one the Qt build uses)."""
    return UiSettings.load()
