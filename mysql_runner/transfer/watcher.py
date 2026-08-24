"""Background watcher that notices local edits and hands them to a sync.

Editing a file in your own editor and having it appear on the server a moment
later removes the whole upload step from the loop. This is deliberately a
poller rather than a filesystem-notification API: it works identically on a
local disk, a mapped drive and a NAS share (where change notifications are
unreliable at best), it needs no extra dependency, and one second of latency is
imperceptible next to the transfer itself.

A file is only reported once its size and timestamp have stopped moving, so a
large save in progress is never uploaded half-written.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from mysql_runner.transfer.base import local_relative
from mysql_runner.transfer.ignore import IgnoreRules

#: How often to walk the tree, in seconds.
INTERVAL = 1.0

#: Never watch more than this many files; a mistaken root should not spin.
MAX_FILES = 20_000


class ChangeKind(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"


@dataclass(frozen=True)
class Change:
    """One observed difference."""

    kind: ChangeKind
    rel: str
    path: str
    size: int = 0
    modified: float | None = None

    def describe(self) -> str:
        return f"{self.kind.value} {self.rel}"


class DirectoryWatcher:
    """Watches one local directory tree on its own thread."""

    def __init__(
        self,
        root: str,
        on_changes: Callable[[list[Change]], None],
        *,
        interval: float = INTERVAL,
        rules: IgnoreRules | None = None,
        max_files: int = MAX_FILES,
        on_message: Callable[[str], None] | None = None,
        recursive: bool = True,
    ) -> None:
        self._root = root
        self._on_changes = on_changes
        self._on_message = on_message
        self._interval = max(0.2, float(interval))
        self._rules = rules or IgnoreRules.defaults()
        self._max_files = max_files
        #: False watches the files in this directory and nothing below it.
        self._recursive = recursive
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: rel -> (size, mtime) as last reported.
        self._known: dict[str, tuple[int, float]] = {}
        #: rel -> (size, mtime) seen once but not yet stable.
        self._pending: dict[str, tuple[int, float]] = {}
        self._truncated = False

    # ----- lifecycle ------------------------------------------------------
    @property
    def root(self) -> str:
        return self._root

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, prime: bool = True) -> None:
        """Begin watching. ``prime`` treats what is already there as known."""
        if self.running:
            return
        self._stop.clear()
        if prime:
            self._known = self._scan()
        self._thread = threading.Thread(
            target=self._loop, name="watch-local", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    # ----- the loop -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                changes = self.poll()
            except OSError as exc:
                self._message(f"Watch stopped: {exc}")
                return
            if changes:
                try:
                    self._on_changes(changes)
                except Exception as exc:  # a bad callback must not kill the thread
                    self._message(f"Watch handler failed: {exc}")

    def poll(self) -> list[Change]:
        """One pass. Returns the changes that have settled since the last one."""
        current = self._scan()
        changes: list[Change] = []

        for rel, facts in current.items():
            known = self._known.get(rel)
            if known == facts:
                self._pending.pop(rel, None)
                continue
            if self._pending.get(rel) != facts:
                # First sighting of this version - wait for it to stop moving.
                self._pending[rel] = facts
                continue
            # Same size and time two polls running: the write has finished.
            self._pending.pop(rel, None)
            self._known[rel] = facts
            kind = ChangeKind.ADDED if known is None else ChangeKind.MODIFIED
            size, mtime = facts
            changes.append(
                Change(
                    kind=kind,
                    rel=rel,
                    path=os.path.join(self._root, rel.replace("/", os.sep)),
                    size=size,
                    modified=mtime,
                )
            )

        for rel in [key for key in self._known if key not in current]:
            del self._known[rel]
            self._pending.pop(rel, None)
            changes.append(
                Change(
                    kind=ChangeKind.REMOVED,
                    rel=rel,
                    path=os.path.join(self._root, rel.replace("/", os.sep)),
                )
            )
        return changes

    def _scan(self) -> dict[str, tuple[int, float]]:
        found: dict[str, tuple[int, float]] = {}
        for current, dirnames, filenames in os.walk(self._root):
            rel_dir = local_relative(self._root, current)
            rel_dir = "" if rel_dir == "." else rel_dir
            keep: list[str] = []
            for name in dirnames:
                child = f"{rel_dir}/{name}" if rel_dir else name
                if self._rules.is_ignored(child, is_dir=True):
                    continue
                if os.path.islink(os.path.join(current, name)):
                    continue
                keep.append(name)
            dirnames[:] = keep if self._recursive else []
            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if self._rules.is_ignored(rel):
                    continue
                if len(found) >= self._max_files:
                    if not self._truncated:
                        self._truncated = True
                        self._message(
                            f"Watching only the first {self._max_files} files "
                            "under this folder."
                        )
                    return found
                try:
                    stat_result = os.stat(os.path.join(current, name))
                except OSError:
                    continue
                found[rel] = (stat_result.st_size, stat_result.st_mtime)
        return found

    def _message(self, text: str) -> None:
        if self._on_message is not None:
            self._on_message(text)


def changed_paths(changes: list[Change]) -> list[str]:
    """The local paths worth uploading out of a change batch."""
    return [
        change.path
        for change in changes
        if change.kind in (ChangeKind.ADDED, ChangeKind.MODIFIED)
    ]


def summarise(changes: list[Change], *, limit: int = 3) -> str:
    """A short status line for a batch of changes."""
    if not changes:
        return "no changes"
    names = [change.rel for change in changes[:limit]]
    text = ", ".join(names)
    if len(changes) > limit:
        text += f" and {len(changes) - limit} more"
    return text


def wait_for_quiet(watcher: DirectoryWatcher, *, timeout: float = 5.0) -> bool:
    """Poll until nothing changes, for tests and for a one-shot sync."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not watcher.poll():
            return True
        time.sleep(0.05)
    return False
