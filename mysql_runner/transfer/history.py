"""Shadow backups and the undo journal behind "un-replace".

Overwriting a file during a deploy is the one destructive act a file manager
performs constantly, and the only one with no natural undo. So before a
transfer replaces anything, the bytes that are about to be lost are copied into
a local cache and the swap is written to a journal. "Undo" then means putting
the cached copy back where it came from.

The cache lives under ``%APPDATA%\\Sitekeeper\\history`` and is pruned by age,
count and total size, so it cannot grow without bound. It holds file contents
from remote servers, which is exactly the sensitivity of the files themselves -
worth knowing when choosing a retention.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum

from mysql_runner.paths import app_data_dir
from mysql_runner.transfer.base import RemoteFS, TransferError

#: Retention defaults; all three are enforced on every prune.
MAX_ENTRIES = 400
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_AGE_DAYS = 30

#: Files bigger than this are journalled but not copied - the backup would cost
#: more than it is worth. The entry records that, so the UI can say so.
MAX_BACKUP_BYTES = 64 * 1024 * 1024

#: One lock per journal file, shared by every store in this process. The
#: transfer pool records from several worker threads at once - and every open
#: tab has its own store pointing at the same journal - so unguarded writes
#: collided on the temp file and surfaced as failed transfers.
_journal_locks: dict[str, threading.RLock] = {}
_journal_locks_guard = threading.Lock()


def _lock_for(journal: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(journal))
    with _journal_locks_guard:
        lock = _journal_locks.get(key)
        if lock is None:
            lock = _journal_locks[key] = threading.RLock()
        return lock


class Action(str, Enum):
    """What happened to the file the journal entry is about."""

    REPLACED_REMOTE = "replaced_remote"  # An upload overwrote a server file.
    REPLACED_LOCAL = "replaced_local"    # A download overwrote a local file.
    DELETED_REMOTE = "deleted_remote"
    DELETED_LOCAL = "deleted_local"


@dataclass
class HistoryEntry:
    """One reversible event."""

    id: str
    action: Action
    profile_id: str
    profile_label: str
    #: The path that was overwritten or deleted.
    target: str
    #: Where the previous bytes are kept, "" when they were not kept.
    backup: str = ""
    size: int = 0
    when: float = field(default_factory=time.time)
    note: str = ""
    undone: bool = False

    @property
    def is_remote(self) -> bool:
        return self.action in (Action.REPLACED_REMOTE, Action.DELETED_REMOTE)

    @property
    def can_undo(self) -> bool:
        return bool(self.backup) and not self.undone and os.path.isfile(self.backup)

    @property
    def name(self) -> str:
        return os.path.basename(self.target.replace("\\", "/").rstrip("/"))

    def describe(self) -> str:
        verb = {
            Action.REPLACED_REMOTE: "replaced on the server",
            Action.REPLACED_LOCAL: "replaced locally",
            Action.DELETED_REMOTE: "deleted on the server",
            Action.DELETED_LOCAL: "deleted locally",
        }[self.action]
        return f"{self.name} {verb}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["action"] = self.action.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            id=data.get("id", uuid.uuid4().hex),
            action=Action(data.get("action", Action.REPLACED_REMOTE.value)),
            profile_id=data.get("profile_id", ""),
            profile_label=data.get("profile_label", ""),
            target=data.get("target", ""),
            backup=data.get("backup", ""),
            size=int(data.get("size", 0) or 0),
            when=float(data.get("when", 0.0) or 0.0),
            note=data.get("note", ""),
            undone=bool(data.get("undone", False)),
        )


class HistoryStore:
    """The journal plus the file cache it points at."""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self._root = str(root) if root else str(app_data_dir() / "history")
        self._journal = os.path.join(self._root, "journal.json")
        self._entries: list[HistoryEntry] = []
        self._loaded = False
        self._lock = _lock_for(self._journal)

    # ----- persistence ----------------------------------------------------
    @property
    def root(self) -> str:
        return self._root

    def load(self) -> list[HistoryEntry]:
        with self._lock:
            if self._loaded:
                return self._entries
            self._loaded = True
            try:
                raw = json.loads(open(self._journal, encoding="utf-8").read())
            except (OSError, ValueError):
                self._entries = []
                return self._entries
            if not isinstance(raw, list):
                self._entries = []
                return self._entries
            entries: list[HistoryEntry] = []
            for item in raw:
                if isinstance(item, dict):
                    try:
                        entries.append(HistoryEntry.from_dict(item))
                    except (ValueError, KeyError):
                        continue
            entries.sort(key=lambda e: e.when, reverse=True)
            self._entries = entries
            return self._entries

    def _reload(self) -> list[HistoryEntry]:
        """Read the journal fresh, picking up what other stores wrote."""
        with self._lock:
            self._loaded = False
            return self.load()

    def save(self) -> None:
        with self._lock:
            os.makedirs(self._root, exist_ok=True)
            payload = json.dumps(
                [entry.to_dict() for entry in self._entries], indent=2
            )
            # A name of its own per write: the lock covers this process, but a
            # fixed name would still collide with anything else holding the
            # file open, and Windows turns that into a sharing violation.
            temp = f"{self._journal}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                with open(temp, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(temp, self._journal)
            except OSError:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise

    # ----- queries --------------------------------------------------------
    def entries(self, *, profile_id: str = "", limit: int = 200) -> list[HistoryEntry]:
        found = [
            entry
            for entry in self.load()
            if not profile_id or entry.profile_id == profile_id
        ]
        return found[:limit]

    def latest_undoable(self, *, profile_id: str = "") -> HistoryEntry | None:
        """The newest entry that can still be put back - what Undo acts on."""
        for entry in self.entries(profile_id=profile_id):
            if entry.can_undo:
                return entry
        return None

    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.load() if entry.backup)

    # ----- recording ------------------------------------------------------
    def record(self, entry: HistoryEntry) -> HistoryEntry:
        with self._lock:
            # Re-read first: another tab's store may have recorded since this
            # one loaded, and saving a stale list would silently drop those.
            self._reload()
            self._entries.insert(0, entry)
            self.prune(save=False)
            self.save()
            return entry

    def backup_slot(self, name: str) -> str:
        """A fresh path inside the cache for one saved file."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = os.path.join(self._root, f"{stamp}-{uuid.uuid4().hex[:8]}")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, name or "file")

    def keep_local_copy(self, path: str) -> tuple[str, int, str]:
        """Copy a local file into the cache. Returns (backup, size, note)."""
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return "", 0, f"could not be read ({exc})"
        if size > MAX_BACKUP_BYTES:
            return "", size, "too large to keep a shadow copy"
        slot = self.backup_slot(os.path.basename(path))
        try:
            shutil.copy2(path, slot)
        except OSError as exc:
            return "", size, f"could not be copied ({exc})"
        return slot, size, ""

    def keep_remote_copy(self, fs: RemoteFS, path: str, *, size: int = -1) -> tuple[str, int, str]:
        """Download the current server copy into the cache before it is lost."""
        if size < 0:
            try:
                size = fs.stat(path).size
            except TransferError:
                return "", 0, "does not exist yet"
        if size > MAX_BACKUP_BYTES:
            return "", size, "too large to keep a shadow copy"
        slot = self.backup_slot(fs.basename(path))
        try:
            fs.download(path, slot)
        except TransferError as exc:
            return "", size, f"could not be fetched ({exc})"
        return slot, size, ""

    # ----- undo -----------------------------------------------------------
    def undo(self, entry_id: str, fs: RemoteFS | None = None) -> str:
        """Put one saved copy back. Returns a message for the status line."""
        self._reload()
        entry = next((item for item in self._entries if item.id == entry_id), None)
        if entry is None:
            raise TransferError("That history entry is gone.")
        if entry.undone:
            raise TransferError(f"{entry.name} has already been restored.")
        if not entry.backup or not os.path.isfile(entry.backup):
            raise TransferError(
                f"The saved copy of {entry.name} is no longer in the cache."
            )
        if entry.is_remote:
            if fs is None:
                raise TransferError("Connect to the server before undoing this.")
            self._restore_remote(fs, entry)
            where = "on the server"
        else:
            self._restore_local(entry)
            where = "locally"
        with self._lock:
            # The restore ran without the lock (it can take a while); find the
            # entry again in case a record() replaced the list meanwhile.
            self._reload()
            fresh = next(
                (item for item in self._entries if item.id == entry_id), None
            )
            if fresh is not None:
                fresh.undone = True
                self.save()
        return f"Restored {entry.name} {where}."

    def _restore_remote(self, fs: RemoteFS, entry: HistoryEntry) -> None:
        from mysql_runner.transfer.base import temp_name

        scratch = temp_name(entry.target, uuid.uuid4().hex[:8])
        fs.upload(entry.backup, scratch)
        try:
            fs.replace(scratch, entry.target)
        except TransferError:
            try:
                fs.remove(scratch)
            except TransferError:
                pass
            raise

    def _restore_local(self, entry: HistoryEntry) -> None:
        target = entry.target
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        temp = f"{target}.mrundo-{uuid.uuid4().hex[:8]}"
        try:
            shutil.copy2(entry.backup, temp)
            os.replace(temp, target)
        except OSError as exc:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise TransferError(f"{entry.name}: {exc}") from exc

    # ----- housekeeping ---------------------------------------------------
    def prune(
        self,
        *,
        max_entries: int = MAX_ENTRIES,
        max_bytes: int = MAX_TOTAL_BYTES,
        max_age_days: int = MAX_AGE_DAYS,
        save: bool = True,
    ) -> int:
        """Drop entries past the retention limits. Returns how many went."""
        with self._lock:
            self.load()
            cutoff = time.time() - max_age_days * 86400
            keep: list[HistoryEntry] = []
            dropped: list[HistoryEntry] = []
            running = 0
            for entry in self._entries:  # newest first
                too_old = entry.when < cutoff
                too_many = len(keep) >= max_entries
                running += entry.size if entry.backup else 0
                too_big = running > max_bytes
                if too_old or too_many or too_big:
                    dropped.append(entry)
                    continue
                keep.append(entry)
            for entry in dropped:
                self._discard_backup(entry)
            self._entries = keep
            if dropped and save:
                self.save()
            return len(dropped)

    def clear(self) -> int:
        """Forget everything and delete every cached copy."""
        with self._lock:
            self._reload()
            count = len(self._entries)
            for entry in self._entries:
                self._discard_backup(entry)
            self._entries = []
            self.save()
            return count

    def _discard_backup(self, entry: HistoryEntry) -> None:
        if not entry.backup:
            return
        folder = os.path.dirname(entry.backup)
        try:
            os.unlink(entry.backup)
        except OSError:
            pass
        # Each backup has its own folder; remove it once it is empty.
        if folder and folder != self._root:
            try:
                os.rmdir(folder)
            except OSError:
                pass


def make_entry(
    action: Action,
    *,
    profile_id: str,
    profile_label: str,
    target: str,
    backup: str = "",
    size: int = 0,
    note: str = "",
) -> HistoryEntry:
    """Build a journal entry with a fresh id and the current time."""
    return HistoryEntry(
        id=uuid.uuid4().hex,
        action=action,
        profile_id=profile_id,
        profile_label=profile_label,
        target=target,
        backup=backup,
        size=size,
        note=note,
    )
