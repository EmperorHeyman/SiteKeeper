"""File and folder digests, and the two-sided comparison built on them.

Comparing by size and timestamp is fast but lies: a file edited in place can
keep its size, and FTP timestamps are frequently wrong by a whole timezone.
Hashing both sides is slower but tells the truth, so both modes exist and the
caller picks.

Folder digests are a Merkle-style roll-up: the digest of a directory is the
hash of its children's ``name\\0digest`` lines, so one changed byte anywhere
below changes the folder's digest, and two folders with equal digests really do
hold the same bytes.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum

from mysql_runner.transfer.base import (
    CHUNK,
    Capability,
    RemoteFS,
    TransferError,
    local_relative,
    relative_posix,
)
from mysql_runner.transfer.ignore import IgnoreRules

#: Default digest. sha256sum and shasum are present on essentially every host.
ALGORITHM = "sha256"

#: Timestamps from FTP servers are routinely a second or two out even when the
#: file is identical, so quick comparisons allow this much slack.
MTIME_TOLERANCE = 2.0

#: Refuse to walk unbounded trees; the UI reports the cap rather than hanging.
MAX_FILES = 20_000


class DiffStatus(str, Enum):
    """How one path compares between the two sides."""

    SAME = "same"
    DIFFERENT = "different"
    LOCAL_ONLY = "local_only"
    REMOTE_ONLY = "remote_only"
    #: Present on both sides but not comparable (no hash, no usable time).
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileInfo:
    """One file in a snapshot."""

    rel: str
    size: int = 0
    modified: float | None = None
    digest: str = ""
    is_dir: bool = False


@dataclass
class TreeSnapshot:
    """Every file below one root, keyed by its relative path."""

    root: str
    files: dict[str, FileInfo] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    #: True when the walk stopped at MAX_FILES rather than finishing.
    truncated: bool = False
    #: Paths that could not be read, with the reason.
    errors: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(info.size for info in self.files.values())

    def digest_of(self, rel: str = "") -> str:
        """Roll-up digest of a directory (or the whole tree when rel is "")."""
        prefix = f"{rel}/" if rel else ""
        lines: list[bytes] = []
        for key in sorted(self.files):
            if prefix and not key.startswith(prefix):
                continue
            info = self.files[key]
            child = key[len(prefix):]
            lines.append(child.encode("utf-8") + b"\0" + info.digest.encode("ascii"))
        if not lines:
            return ""
        return hashlib.sha256(b"\n".join(lines)).hexdigest()

    def child_names(self, rel: str = "") -> set[str]:
        """Immediate children of a directory inside this snapshot."""
        prefix = f"{rel}/" if rel else ""
        names: set[str] = set()
        for key in list(self.files) + sorted(self.dirs):
            if prefix and not key.startswith(prefix):
                continue
            tail = key[len(prefix):]
            if tail:
                names.add(tail.split("/", 1)[0])
        return names


# ----- local side ---------------------------------------------------------
def hash_local_file(path: str, *, algorithm: str = ALGORITHM, progress=None) -> str:
    """Digest one local file."""
    digest = hashlib.new(algorithm)
    done = 0
    try:
        total = os.path.getsize(path)
    except OSError:
        total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except OSError as exc:
        raise TransferError(f"{path}: {exc}") from exc
    return digest.hexdigest()


def snapshot_local(
    root: str,
    *,
    rules: IgnoreRules | None = None,
    with_hashes: bool = True,
    max_files: int = MAX_FILES,
    algorithm: str = ALGORITHM,
    on_progress=None,
    recursive: bool = True,
) -> TreeSnapshot:
    """Walk a local directory into a snapshot.

    ``recursive=False`` looks at the files in ``root`` and stops there, which is
    what makes a sync of a site root usable: the loose files at the top are
    deployable, the directories under them are somebody else's business.
    """
    snapshot = TreeSnapshot(root=root)
    rules = rules or IgnoreRules.empty()
    for current, dirnames, filenames in os.walk(root):
        base_rel = local_relative(root, current)
        base_rel = "" if base_rel == "." else base_rel
        # Prune ignored directories in place so os.walk never enters them.
        dirnames[:] = _keep_dirs(snapshot, rules, current, base_rel, dirnames)
        if not recursive:
            # Do not record them either: a directory this snapshot never looked
            # inside must not read as "empty on this side" to a comparison.
            dirnames[:] = []
            snapshot.dirs.clear()
        for name in sorted(filenames):
            rel = f"{base_rel}/{name}" if base_rel else name
            if rules.is_ignored(rel):
                continue
            if len(snapshot.files) >= max_files:
                snapshot.truncated = True
                return snapshot
            info = _local_file_info(
                snapshot, os.path.join(current, name), rel, with_hashes, algorithm
            )
            if info is None:
                continue
            snapshot.files[rel] = info
            if on_progress is not None:
                on_progress(rel, len(snapshot.files))
    return snapshot


def _keep_dirs(
    snapshot: TreeSnapshot,
    rules: IgnoreRules,
    current: str,
    base_rel: str,
    dirnames: list[str],
) -> list[str]:
    """Subdirectories worth descending into, recording them on the way."""
    keep: list[str] = []
    for name in sorted(dirnames):
        rel = f"{base_rel}/{name}" if base_rel else name
        if rules.is_ignored(rel, is_dir=True):
            continue
        if os.path.islink(os.path.join(current, name)):
            continue  # Never recurse into a link; it may point upwards.
        keep.append(name)
        snapshot.dirs.add(rel)
    return keep


def _local_file_info(
    snapshot: TreeSnapshot,
    full: str,
    rel: str,
    with_hashes: bool,
    algorithm: str,
) -> FileInfo | None:
    """Stat (and optionally hash) one local file, logging what fails."""
    try:
        stat_result = os.stat(full)
    except OSError as exc:
        snapshot.errors.append(f"{rel}: {exc}")
        return None
    digest = ""
    if with_hashes:
        try:
            digest = hash_local_file(full, algorithm=algorithm)
        except TransferError as exc:
            snapshot.errors.append(str(exc))
    return FileInfo(
        rel=rel,
        size=stat_result.st_size,
        modified=stat_result.st_mtime,
        digest=digest,
    )


# ----- remote side --------------------------------------------------------
def hash_remote_file(
    fs: RemoteFS, path: str, *, algorithm: str = ALGORITHM, progress=None
) -> str:
    """Digest one remote file, server-side when the protocol allows it."""
    if fs.supports(Capability.EXEC):
        from mysql_runner.transfer.remote_exec import remote_digest

        digest = remote_digest(fs, path, algorithm=algorithm)
        if digest:
            return digest
    # No shell (or no digest tool): stream the bytes and hash them here.
    accumulator = hashlib.new(algorithm)
    fs.stream_download(path, accumulator.update, progress)
    return accumulator.hexdigest()


def snapshot_remote(
    fs: RemoteFS,
    root: str,
    *,
    rules: IgnoreRules | None = None,
    with_hashes: bool = True,
    max_files: int = MAX_FILES,
    algorithm: str = ALGORITHM,
    on_progress=None,
    prefer_exec: bool = True,
    recursive: bool = True,
) -> TreeSnapshot:
    """Walk a remote directory into a snapshot.

    With a shell available the whole tree is hashed by one remote command,
    which turns thousands of round trips into one. Otherwise the tree is walked
    over the transfer channel and files are hashed by streaming them.

    ``recursive=False`` reads only the directory itself, to match a local
    snapshot taken the same way.
    """
    rules = rules or IgnoreRules.empty()
    if with_hashes and prefer_exec and recursive and fs.supports(Capability.EXEC):
        from mysql_runner.transfer.remote_exec import digest_tree

        snapshot = digest_tree(fs, root, algorithm=algorithm, max_files=max_files)
        if snapshot is not None:
            _apply_rules(snapshot, rules)
            return snapshot

    snapshot = TreeSnapshot(root=root)
    for current, entries in fs.walk(root):
        base_rel = relative_posix(root, current)
        if not recursive and base_rel:
            break  # fs.walk yields the root first, so this is a subdirectory
        for entry in entries:
            rel = f"{base_rel}/{entry.name}" if base_rel else entry.name
            if rules.is_ignored(rel, is_dir=entry.is_dir):
                continue
            if entry.is_dir:
                if recursive:
                    snapshot.dirs.add(rel)
                continue
            if len(snapshot.files) >= max_files:
                snapshot.truncated = True
                return snapshot
            digest = ""
            if with_hashes:
                digest = _remote_digest_or_note(
                    snapshot, fs, fs.join(current, entry.name), rel, algorithm
                )
            snapshot.files[rel] = FileInfo(
                rel=rel,
                size=entry.size,
                modified=entry.modified,
                digest=digest,
            )
            if on_progress is not None:
                on_progress(rel, len(snapshot.files))
    return snapshot


def _remote_digest_or_note(
    snapshot: TreeSnapshot, fs: RemoteFS, path: str, rel: str, algorithm: str
) -> str:
    """Hash one remote file; record the failure and carry on if it cannot be."""
    try:
        return hash_remote_file(fs, path, algorithm=algorithm)
    except TransferError as exc:
        snapshot.errors.append(f"{rel}: {exc}")
        return ""


def _apply_rules(snapshot: TreeSnapshot, rules: IgnoreRules) -> None:
    """Drop entries the rule set excludes (used after a bulk remote hash)."""
    if not rules:
        return
    excluded = [rel for rel in snapshot.files if rules.is_ignored(rel)]
    for rel in excluded:
        del snapshot.files[rel]
    snapshot.dirs = {rel for rel in snapshot.dirs if not rules.is_ignored(rel, is_dir=True)}


# ----- comparison ---------------------------------------------------------
@dataclass
class DiffReport:
    """The verdict for every path seen on either side."""

    statuses: dict[str, DiffStatus] = field(default_factory=dict)
    local: TreeSnapshot | None = None
    remote: TreeSnapshot | None = None
    compared_by: str = "hash"

    # ----- queries --------------------------------------------------------
    def status(self, rel: str) -> DiffStatus | None:
        return self.statuses.get(rel)

    def status_of_name(self, base_rel: str, name: str, *, is_dir: bool) -> DiffStatus | None:
        """The verdict for one row of a directory listing."""
        rel = f"{base_rel}/{name}" if base_rel else name
        if not is_dir:
            return self.statuses.get(rel)
        return self.directory_status(rel)

    def directory_status(self, rel: str) -> DiffStatus | None:
        """Roll a directory's children up into one verdict."""
        prefix = f"{rel}/" if rel else ""
        seen: set[DiffStatus] = set()
        for key, status in self.statuses.items():
            if prefix and not key.startswith(prefix):
                continue
            seen.add(status)
        if not seen:
            return None
        if seen == {DiffStatus.SAME}:
            return DiffStatus.SAME
        if seen == {DiffStatus.LOCAL_ONLY}:
            return DiffStatus.LOCAL_ONLY
        if seen == {DiffStatus.REMOTE_ONLY}:
            return DiffStatus.REMOTE_ONLY
        return DiffStatus.DIFFERENT

    def paths(self, *statuses: DiffStatus) -> list[str]:
        wanted = set(statuses)
        return sorted(rel for rel, status in self.statuses.items() if status in wanted)

    def to_upload(self) -> list[str]:
        """Files the local side should push (newer or missing there)."""
        return self.paths(DiffStatus.LOCAL_ONLY, DiffStatus.DIFFERENT)

    def to_download(self) -> list[str]:
        """Files only the server has."""
        return self.paths(DiffStatus.REMOTE_ONLY, DiffStatus.DIFFERENT)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {status.value: 0 for status in DiffStatus}
        for status in self.statuses.values():
            tally[status.value] += 1
        return tally

    def summary(self) -> str:
        tally = self.counts()
        return (
            f"{tally['same']} identical, {tally['different']} differing, "
            f"{tally['local_only']} only local, {tally['remote_only']} only remote"
        )


def compare(
    local: TreeSnapshot,
    remote: TreeSnapshot,
    *,
    tolerance: float = MTIME_TOLERANCE,
) -> DiffReport:
    """Compare two snapshots path by path."""
    report = DiffReport(local=local, remote=remote)
    by_hash = _both_hashed(local, remote)
    report.compared_by = "hash" if by_hash else "size and time"
    for rel in set(local.files) | set(remote.files):
        left = local.files.get(rel)
        right = remote.files.get(rel)
        if left is None:
            report.statuses[rel] = DiffStatus.REMOTE_ONLY
            continue
        if right is None:
            report.statuses[rel] = DiffStatus.LOCAL_ONLY
            continue
        report.statuses[rel] = _compare_one(left, right, tolerance)
    return report


def _both_hashed(local: TreeSnapshot, remote: TreeSnapshot) -> bool:
    """True when every file seen on either side carries a digest."""
    infos = list(local.files.values()) + list(remote.files.values())
    return bool(infos) and all(info.digest for info in infos)


def _compare_one(left: FileInfo, right: FileInfo, tolerance: float) -> DiffStatus:
    if left.digest and right.digest:
        return DiffStatus.SAME if left.digest == right.digest else DiffStatus.DIFFERENT
    if left.size != right.size:
        return DiffStatus.DIFFERENT
    if left.modified is None or right.modified is None:
        # Same size, no usable timestamps: say so instead of guessing "same".
        return DiffStatus.UNKNOWN
    if abs(left.modified - right.modified) <= tolerance:
        return DiffStatus.SAME
    return DiffStatus.DIFFERENT
