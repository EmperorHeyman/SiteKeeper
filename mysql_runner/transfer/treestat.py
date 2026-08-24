"""Recursive folder statistics: real size, and a modified date that is true.

Servers report a directory's own mtime, which changes only when an entry is
added or removed directly inside it - so a file edited three levels down leaves
every parent looking untouched. That is misleading in a file manager, so a
folder here reports the newest timestamp anywhere below it, and its size is the
sum of what it contains.

Walking a tree is expensive, so results are cached per session and computed on
demand: the listing appears immediately with the server's own dates, and the
corrected ones replace them as they arrive.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace

from mysql_runner.transfer.base import Capability, RemoteEntry, RemoteFS, TransferError

#: Give up after this many entries so one huge folder cannot stall a listing.
MAX_ENTRIES = 50_000

#: How long a computed folder summary stays fresh, in seconds.
CACHE_TTL = 60.0


@dataclass(frozen=True)
class FolderStats:
    """What one directory contains, all the way down."""

    path: str
    size: int = 0
    files: int = 0
    dirs: int = 0
    newest: float | None = None
    truncated: bool = False

    def merged_entry(self, entry: RemoteEntry) -> RemoteEntry:
        """The listing row for this folder, with the corrected size and date."""
        newest = self.newest if self.newest is not None else entry.modified
        return replace(entry, size=self.size, modified=newest)

    def describe(self) -> str:
        suffix = " (partial)" if self.truncated else ""
        return f"{self.files} files, {self.dirs} folders{suffix}"


class FolderStatsCache:
    """Per-session memo of folder summaries, keyed by side and path."""

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        self._values: dict[tuple[str, str], tuple[float, FolderStats]] = {}

    def get(self, side: str, path: str) -> FolderStats | None:
        found = self._values.get((side, path))
        if found is None:
            return None
        stamp, stats = found
        if time.monotonic() - stamp > self._ttl:
            del self._values[(side, path)]
            return None
        return stats

    def put(self, side: str, path: str, stats: FolderStats) -> None:
        self._values[(side, path)] = (time.monotonic(), stats)

    def invalidate(self, side: str = "", path: str = "") -> None:
        """Forget one path (and its descendants), one side, or everything."""
        if not side:
            self._values.clear()
            return
        if not path:
            for key in [k for k in self._values if k[0] == side]:
                del self._values[key]
            return
        prefix = path.rstrip("/") + "/"
        for key in [
            k
            for k in self._values
            if k[0] == side and (k[1] == path or k[1].startswith(prefix))
        ]:
            del self._values[key]


# ----- local --------------------------------------------------------------
def local_folder_stats(path: str, *, max_entries: int = MAX_ENTRIES) -> FolderStats:
    """Summarise a local directory tree."""
    size = 0
    files = 0
    dirs = 0
    newest: float | None = None
    seen = 0
    truncated = False
    for current, dirnames, filenames in os.walk(path):
        dirs += len(dirnames)
        for name in filenames:
            seen += 1
            if seen > max_entries:
                truncated = True
                break
            try:
                stat_result = os.stat(os.path.join(current, name))
            except OSError:
                continue
            files += 1
            size += stat_result.st_size
            if newest is None or stat_result.st_mtime > newest:
                newest = stat_result.st_mtime
        if truncated:
            break
    if newest is None:
        try:
            newest = os.stat(path).st_mtime
        except OSError:
            newest = None
    return FolderStats(
        path=path,
        size=size,
        files=files,
        dirs=dirs,
        newest=newest,
        truncated=truncated,
    )


# ----- remote -------------------------------------------------------------
def remote_folder_stats(
    fs: RemoteFS,
    path: str,
    *,
    max_entries: int = MAX_ENTRIES,
    prefer_exec: bool = True,
) -> FolderStats:
    """Summarise a remote directory tree, server-side when possible."""
    if prefer_exec and fs.supports(Capability.EXEC):
        stats = _stats_via_find(fs, path)
        if stats is not None:
            return stats
    return _stats_via_walk(fs, path, max_entries)


def _stats_via_find(fs: RemoteFS, path: str) -> FolderStats | None:
    """One find(1) call for the whole subtree. None when find cannot do it."""
    from mysql_runner.transfer.remote_exec import quote, run

    command = f"find {quote(path)} -printf '%y\\t%s\\t%T@\\n' 2>/dev/null"
    try:
        result = run(fs, command, timeout=300)
    except TransferError:
        return None
    if not result.ok or not result.stdout.strip():
        return None  # BSD find lacks -printf; fall back to walking.
    size = 0
    files = 0
    dirs = 0
    newest: float | None = None
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        kind, size_text, mtime_text = parts
        try:
            mtime = float(mtime_text)
        except ValueError:
            mtime = 0.0
        if newest is None or mtime > newest:
            newest = mtime
        if kind == "d":
            dirs += 1
            continue
        files += 1
        try:
            size += int(size_text)
        except ValueError:
            pass
    # find counts the directory itself.
    dirs = max(0, dirs - 1)
    return FolderStats(path=path, size=size, files=files, dirs=dirs, newest=newest)


def _stats_via_walk(fs: RemoteFS, path: str, max_entries: int) -> FolderStats:
    size = 0
    files = 0
    dirs = 0
    newest: float | None = None
    seen = 0
    truncated = False
    for _current, entries in fs.walk(path):
        for entry in entries:
            seen += 1
            if seen > max_entries:
                truncated = True
                break
            if entry.modified is not None and (newest is None or entry.modified > newest):
                newest = entry.modified
            if entry.is_dir:
                dirs += 1
            else:
                files += 1
                size += entry.size
        if truncated:
            break
    return FolderStats(
        path=path, size=size, files=files, dirs=dirs, newest=newest, truncated=truncated
    )


# ----- listing integration -----------------------------------------------
def apply_folder_stats(
    entries: list[RemoteEntry], stats: dict[str, FolderStats]
) -> list[RemoteEntry]:
    """Replace directory rows with corrected size and modified time."""
    out: list[RemoteEntry] = []
    for entry in entries:
        found = stats.get(entry.name) if entry.is_dir else None
        out.append(found.merged_entry(entry) if found is not None else entry)
    return out
