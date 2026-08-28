"""Encrypted persistence of server profiles."""

from __future__ import annotations

import json
import os

from cryptography.fernet import InvalidToken

from mysql_runner.crypto.vault import Vault
from mysql_runner.paths import servers_path
from mysql_runner.storage.models import ServerProfile


class StoreError(Exception):
    """Raised when the server store cannot be read or decrypted."""


def opens_store(vault: "Vault") -> bool:
    """Whether ``vault``'s key can actually decrypt the stored profiles.

    The OS keyring cache can go stale - it is keyed by application name, not by
    which vault file it belongs to, so a key cached by another install (or by a
    test run) will load happily and then fail to decrypt anything. Callers use
    this to validate a cached key before trusting it, instead of discovering the
    mismatch as a fatal error at startup.
    """
    try:
        ServerStore(vault)
    except StoreError:
        return False
    return True


class ServerStore:
    """Loads and saves :class:`ServerProfile` records encrypted with the vault DEK."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._profiles: list[ServerProfile] = []
        self.load()

    # ----- persistence ---------------------------------------------------
    def load(self) -> None:
        path = servers_path()
        if not path.exists():
            self._profiles = []
            return
        token = path.read_bytes()
        if not token:
            self._profiles = []
            return
        try:
            plaintext = self._vault.decrypt(token)
        except InvalidToken as exc:
            raise StoreError("Server store could not be decrypted.") from exc
        raw = json.loads(plaintext.decode("utf-8"))
        self._profiles = [ServerProfile.from_dict(item) for item in raw]

    def save(self) -> None:
        raw = [p.to_dict() for p in self._profiles]
        plaintext = json.dumps(raw).encode("utf-8")
        token = self._vault.encrypt(plaintext)
        path = servers_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(token)
        os.replace(tmp, path)

    # ----- CRUD ----------------------------------------------------------
    def all(self) -> list[ServerProfile]:
        return list(self._profiles)

    def get(self, profile_id: str) -> ServerProfile | None:
        return next((p for p in self._profiles if p.id == profile_id), None)

    def add(self, profile: ServerProfile) -> None:
        self._profiles.append(profile)
        self.save()

    def add_many(self, profiles: list[ServerProfile]) -> int:
        """Append several profiles at once. Returns the number added."""
        if not profiles:
            return 0
        self._profiles.extend(profiles)
        self.save()
        return len(profiles)

    def update(self, profile: ServerProfile) -> None:
        for index, existing in enumerate(self._profiles):
            if existing.id == profile.id:
                self._profiles[index] = profile
                self.save()
                return
        raise KeyError(profile.id)

    def reorder(self, positions: dict[str, tuple[str, int]]) -> int:
        """Apply a hand-made arrangement: id -> (group, position within it).

        One write for the whole list rather than one per moved row, because a
        drag can change the group and the position of everything below it in
        two lists at once, and saving that a row at a time would leave the
        vault briefly describing an order nobody asked for.
        """
        changed = 0
        for profile in self._profiles:
            found = positions.get(profile.id)
            if found is None:
                continue
            group, order = found
            if profile.group == group and profile.order == order:
                continue
            profile.group = group
            profile.order = order
            changed += 1
        if changed:
            self.save()
        return changed

    def delete(self, profile_id: str) -> None:
        self._profiles = [p for p in self._profiles if p.id != profile_id]
        self.save()
