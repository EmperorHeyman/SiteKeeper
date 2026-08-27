"""Multi-threaded transfer engine: several files at once, with a real queue.

One connection copying one file at a time wastes most of a link's capacity,
because each small file costs a round trip that nothing overlaps. This pool
opens a handful of *separate* connections and runs a job on each, which is the
difference between minutes and seconds on a tree of small files.

Three properties matter as much as the speed:

* **The navigation connection is never used for transfers.** Browsing stays
  responsive while a queue runs, because the pool's connections are its own.
* **The queue is controllable.** Items can be paused, resumed, reordered and
  cancelled individually, mid-file, not just between files.
* **Overwrites are safe.** With atomic uploads on, bytes go to a scratch name
  and are renamed into place, so a half-written file is never live; with shadow
  backups on, whatever was there is kept first so it can be put back.

Everything here is Qt-free: progress arrives through plain callables, which the
Qt tab turns into signals and the FastAPI backend turns into WebSocket events.
Callbacks run on worker threads - whatever receives them must be thread-safe.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from mysql_runner.transfer.base import (
    Capability,
    ProgressCallback,
    RemoteFS,
    TransferError,
    local_relative,
    relative_posix,
    temp_name,
)
from mysql_runner.transfer.history import Action, HistoryStore, make_entry
from mysql_runner.transfer.ignore import IgnoreRules

#: Priorities. Lower runs sooner; the queue is FIFO within one priority.
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 5
PRIORITY_LOW = 9

#: Default number of parallel connections. Three is polite to shared hosting
#: and still roughly triples throughput on small files.
DEFAULT_WORKERS = 3
MAX_WORKERS = 16

#: Progress is reported at most this often per file, to keep the UI cheap.
PROGRESS_INTERVAL = 0.1


class JobState(str, Enum):
    """Where one queued file has got to."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.SKIPPED)


class Overwrite(str, Enum):
    """What to do when the destination already has the file."""

    ALWAYS = "always"
    NEWER = "newer"   # Only when the source is newer or a different size.
    NEVER = "never"


@dataclass
class TransferItem:
    """One file in the queue."""

    upload: bool
    local: str
    remote: str
    size: int = 0
    priority: int = PRIORITY_NORMAL
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    transferred: int = 0
    error: str = ""
    started: float | None = None
    finished_at: float | None = None
    #: True when the transfer replaced an existing file.
    replaced: bool = False
    #: Set when the item was skipped or backed up, for the status line.
    note: str = ""
    _sequence: int = 0
    _cancel: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.local) or self.remote.rsplit("/", 1)[-1]

    @property
    def source(self) -> str:
        return self.local if self.upload else self.remote

    @property
    def destination(self) -> str:
        return self.remote if self.upload else self.local

    @property
    def fraction(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(1.0, self.transferred / self.size)

    @property
    def rate(self) -> float:
        """Bytes per second so far, 0 when it has not started."""
        if not self.started:
            return 0.0
        elapsed = (self.finished_at or time.monotonic()) - self.started
        return self.transferred / elapsed if elapsed > 0 else 0.0

    def snapshot(self) -> "TransferItem":
        """A detached copy, safe to hand to the GUI thread."""
        clone = TransferItem(
            upload=self.upload,
            local=self.local,
            remote=self.remote,
            size=self.size,
            priority=self.priority,
            id=self.id,
        )
        clone.state = self.state
        clone.transferred = self.transferred
        clone.error = self.error
        clone.started = self.started
        clone.finished_at = self.finished_at
        clone.replaced = self.replaced
        clone.note = self.note
        clone._sequence = self._sequence
        return clone


@dataclass
class PoolOptions:
    """How the pool behaves. All of it is user-visible in Settings."""

    workers: int = DEFAULT_WORKERS
    #: Upload to a scratch name and rename into place.
    atomic: bool = True
    #: Keep the previous version of anything overwritten.
    keep_backups: bool = True
    overwrite: Overwrite = Overwrite.ALWAYS
    #: Re-read the file after uploading and compare digests.
    verify: bool = False

    def sane(self) -> "PoolOptions":
        self.workers = max(1, min(MAX_WORKERS, int(self.workers or 1)))
        return self


@dataclass
class PoolEvents:
    """Callbacks into whatever is showing the queue. All optional."""

    on_item: Callable[[TransferItem], None] | None = None
    on_progress: Callable[[TransferItem], None] | None = None
    on_message: Callable[[str], None] | None = None
    on_idle: Callable[[dict], None] | None = None

    def item(self, item: TransferItem) -> None:
        if self.on_item is not None:
            self.on_item(item.snapshot())

    def progress(self, item: TransferItem) -> None:
        if self.on_progress is not None:
            self.on_progress(item.snapshot())

    def message(self, text: str) -> None:
        if self.on_message is not None:
            self.on_message(text)

    def idle(self, stats: dict) -> None:
        if self.on_idle is not None:
            self.on_idle(stats)


class _Cancelled(Exception):
    """Raised inside a progress callback to abandon the file being copied."""


class TransferPool:
    """A queue of files and the connections that carry them."""

    def __init__(
        self,
        factory: Callable[[], RemoteFS],
        *,
        options: PoolOptions | None = None,
        events: PoolEvents | None = None,
        history: HistoryStore | None = None,
        profile_id: str = "",
        profile_label: str = "",
    ) -> None:
        self._factory = factory
        self._options = (options or PoolOptions()).sane()
        self._events = events or PoolEvents()
        self._history = history
        self._profile_id = profile_id
        self._profile_label = profile_label

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._items: list[TransferItem] = []
        self._sequence = 0
        self._paused = False
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._connections: dict[int, RemoteFS] = {}
        self._active = 0
        # Worker ids never repeat: a retired worker's slot must not be handed
        # to a new thread while another thread is still using that connection.
        self._next_worker = 0

    # ----- queue ----------------------------------------------------------
    def submit(self, items: list[TransferItem]) -> list[str]:
        """Add files to the queue and make sure workers are running."""
        if not items:
            return []
        with self._condition:
            for item in items:
                self._sequence += 1
                item._sequence = self._sequence
                self._items.append(item)
            self._condition.notify_all()
        for item in items:
            self._events.item(item)
        self._ensure_workers()
        return [item.id for item in items]

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._stopping:
                return
            wanted = min(self._options.workers, max(1, self._queued_count()))
            while len(self._threads) < wanted:
                index = self._next_worker
                self._next_worker += 1
                thread = threading.Thread(
                    target=self._worker,
                    args=(index,),
                    name=f"transfer-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def _queued_count(self) -> int:
        return sum(1 for item in self._items if item.state == JobState.QUEUED)

    # ----- control --------------------------------------------------------
    def pause(self) -> None:
        with self._condition:
            self._paused = True
        self._events.message("Transfers paused.")

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()
        self._events.message("Transfers resumed.")
        self._ensure_workers()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_workers(self, count: int) -> None:
        """Change the connection count; new workers start on the next submit."""
        with self._lock:
            self._options.workers = max(1, min(MAX_WORKERS, int(count)))
        self._ensure_workers()

    def cancel(self, item_id: str) -> bool:
        """Cancel one item, mid-file if it is already running."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state.finished:
                return False
            if item.state == JobState.QUEUED:
                item.state = JobState.CANCELLED
                item.finished_at = time.monotonic()
                self._events.item(item)
                return True
            item._cancel = True
            self._condition.notify_all()
        return True

    def cancel_all(self) -> int:
        """Cancel everything queued and running."""
        cancelled: list[TransferItem] = []
        with self._condition:
            for item in self._items:
                if item.state == JobState.QUEUED:
                    item.state = JobState.CANCELLED
                    item.finished_at = time.monotonic()
                    cancelled.append(item)
                elif item.state == JobState.RUNNING:
                    item._cancel = True
            self._paused = False
            self._condition.notify_all()
        for item in cancelled:
            self._events.item(item)
        return len(cancelled)

    def prioritize(self, item_id: str, *, priority: int = PRIORITY_HIGH) -> bool:
        """Move one queued item up (or down) the queue."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state != JobState.QUEUED:
                return False
            item.priority = priority
            if priority <= PRIORITY_HIGH:
                # Ahead of everything else already marked high.
                self._sequence += 1
                item._sequence = -self._sequence
            self._condition.notify_all()
        self._events.item(item)
        return True

    def reorder(self, item_ids: list[str]) -> None:
        """Apply an explicit order, as produced by dragging rows around."""
        with self._condition:
            positions = {item_id: index for index, item_id in enumerate(item_ids)}
            for item in self._items:
                if item.id in positions and item.state == JobState.QUEUED:
                    item.priority = PRIORITY_NORMAL
                    item._sequence = positions[item.id]
            self._condition.notify_all()

    def clear_finished(self, *, keep_failed: bool = False) -> int:
        """Forget finished items. ``keep_failed`` leaves failures retryable."""
        with self._condition:
            before = len(self._items)
            self._items = [
                item
                for item in self._items
                if not item.state.finished
                or (keep_failed and item.state == JobState.FAILED)
            ]
            return before - len(self._items)

    def retry(self, item_id: str) -> bool:
        """Put one failed (or cancelled) item back in the queue."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state not in (
                JobState.FAILED, JobState.CANCELLED
            ):
                return False
            item.state = JobState.QUEUED
            item.error = ""
            item.note = ""
            item.transferred = 0
            item.started = None
            item.finished_at = None
            item._cancel = False
            self._sequence += 1
            item._sequence = self._sequence
            self._condition.notify_all()
        self._events.item(item)
        self._ensure_workers()
        return True

    def retry_failed(self) -> int:
        """Queue every failed item again. Returns how many were requeued."""
        with self._lock:
            wanted = [
                item.id for item in self._items if item.state == JobState.FAILED
            ]
        return sum(1 for item_id in wanted if self.retry(item_id))

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop the workers and close their connections."""
        with self._condition:
            self._stopping = True
            self._paused = False
            for item in self._items:
                if item.state == JobState.RUNNING:
                    item._cancel = True
            self._condition.notify_all()
        if wait:
            # Copy first: a worker retiring on its own removes itself from this
            # list while we are walking it.
            for thread in self._threads.copy():
                thread.join(timeout)
        with self._lock:
            connections = self._connections.copy()
            self._connections.clear()
            self._threads.clear()
        for connection in connections.values():
            try:
                connection.close()
            except Exception:
                pass

    # ----- inspection -----------------------------------------------------
    def items(self) -> list[TransferItem]:
        with self._lock:
            return [item.snapshot() for item in self._items]

    def stats(self) -> dict:
        with self._lock:
            tally = {state.value: 0 for state in JobState}
            done_bytes = 0
            total_bytes = 0
            for item in self._items:
                tally[item.state.value] += 1
                total_bytes += item.size
                done_bytes += item.size if item.state == JobState.DONE else item.transferred
            return {
                "counts": tally,
                "bytes_done": done_bytes,
                "bytes_total": total_bytes,
                "paused": self._paused,
                "workers": self._options.workers,
                "active": self._active,
            }

    def idle(self) -> bool:
        with self._lock:
            return self._active == 0 and self._queued_count() == 0

    def _find(self, item_id: str) -> TransferItem | None:
        return next((item for item in self._items if item.id == item_id), None)

    # ----- the worker loop ------------------------------------------------
    def _worker(self, index: int) -> None:
        try:
            while True:
                item = self._next_item()
                if item is None:
                    return
                try:
                    connection = self._connection_for(index)
                except TransferError as exc:
                    self._finish(item, JobState.FAILED, error=str(exc))
                    self._events.message(f"Connection {index + 1}: {exc}")
                    # Without a connection this worker can do nothing useful.
                    return
                error = self._run_item(connection, item)
                if error is None:
                    continue
                # A session dropped while the pool sat idle fails whatever it
                # is handed, so check the connection before believing the
                # error - a dead one earns the item one go on a fresh session.
                if self._stopping or item._cancel or connection.alive():
                    self._finish(item, JobState.FAILED, error=error)
                    continue
                self._close_connection(index)
                try:
                    connection = self._connection_for(index)
                except TransferError as exc:
                    self._finish(item, JobState.FAILED, error=str(exc))
                    self._events.message(f"Connection {index + 1}: {exc}")
                    return
                self._events.message("A transfer connection was dropped; reconnected.")
                item.transferred = 0
                error = self._run_item(connection, item)
                if error is not None:
                    self._finish(item, JobState.FAILED, error=error)
        finally:
            self._retire()
            self._close_connection(index)

    def _retire(self) -> None:
        """Take this thread off the roster so a later submit can start a fresh one."""
        with self._lock:
            current = threading.current_thread()
            self._threads = [thread for thread in self._threads if thread is not current]

    def _next_item(self) -> TransferItem | None:
        with self._condition:
            while True:
                if self._stopping:
                    return None
                if not self._paused:
                    item = self._pick()
                    if item is not None:
                        item.state = JobState.RUNNING
                        item.started = time.monotonic()
                        self._active += 1
                        self._events.item(item)
                        return item
                    if self._queued_count() == 0:
                        return  # Nothing left to do; the thread retires. None
                self._condition.wait(0.25)

    def _pick(self) -> TransferItem | None:
        best: TransferItem | None = None
        for item in self._items:
            if item.state != JobState.QUEUED:
                continue
            if best is None or (item.priority, item._sequence) < (best.priority, best._sequence):
                best = item
        return best

    def _connection_for(self, index: int) -> RemoteFS:
        with self._lock:
            existing = self._connections.get(index)
            if existing is not None:
                return existing
        connection = self._factory()
        with self._lock:
            self._connections[index] = connection
        return connection

    def _close_connection(self, index: int) -> None:
        with self._lock:
            connection = self._connections.pop(index, None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    # ----- one file -------------------------------------------------------
    def _run_item(self, fs: RemoteFS, item: TransferItem) -> str | None:
        """Carry one file. A remote failure is *returned*, not finished, so
        the worker loop can decide whether a fresh connection deserves a retry;
        every other outcome (done, skipped, cancelled, local error) is final.
        """
        try:
            if self._should_skip(fs, item):
                self._finish(item, JobState.SKIPPED)
                return None
            self._prepare_destination(item)
            self._backup_destination(fs, item)
            if item.upload:
                self._do_upload(fs, item)
            else:
                self._do_download(fs, item)
        except _Cancelled:
            self._finish(item, JobState.CANCELLED)
            return None
        except TransferError as exc:
            return str(exc) or exc.__class__.__name__
        except OSError as exc:
            self._finish(item, JobState.FAILED, error=str(exc))
            return None
        self._finish(item, JobState.DONE)
        return None

    def _should_skip(self, fs: RemoteFS, item: TransferItem) -> bool:
        mode = self._options.overwrite
        if mode == Overwrite.ALWAYS:
            return False
        target_exists, target_size, target_mtime = self._destination_facts(fs, item)
        if not target_exists:
            return False
        item.replaced = True
        if mode == Overwrite.NEVER:
            item.note = "kept the existing file"
            return True
        source_size, source_mtime = self._source_facts(fs, item)
        if source_size != target_size:
            return False
        if source_mtime is None or target_mtime is None:
            return False
        if source_mtime <= target_mtime + 2.0:
            item.note = "already up to date"
            return True
        return False

    def _destination_facts(self, fs: RemoteFS, item: TransferItem):
        if item.upload:
            try:
                stat = fs.stat(item.remote)
            except TransferError:
                return False, 0, None
            return True, stat.size, stat.modified
        try:
            result = os.stat(item.local)
        except OSError:
            return False, 0, None
        return True, result.st_size, result.st_mtime

    def _source_facts(self, fs: RemoteFS, item: TransferItem):
        if item.upload:
            try:
                result = os.stat(item.local)
            except OSError:
                return item.size, None
            return result.st_size, result.st_mtime
        try:
            stat = fs.stat(item.remote)
        except TransferError:
            return item.size, None
        return stat.size, stat.modified

    def _prepare_destination(self, item: TransferItem) -> None:
        """Make sure a download has somewhere to land."""
        if item.upload:
            return
        parent = os.path.dirname(item.local)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _backup_destination(self, fs: RemoteFS, item: TransferItem) -> None:
        """Keep whatever is about to be overwritten, if that is switched on."""
        if self._history is None or not self._options.keep_backups:
            return
        if item.upload:
            try:
                existing = fs.stat(item.remote)
            except TransferError:
                return
            item.replaced = True
            if self._unchanged(item.local, existing):
                # Replacing a file with an identical copy loses nothing, so
                # the backup - a full download of the old file - is skipped.
                # Re-deploys are mostly unchanged files; this is where the
                # bulk of their time used to go.
                return
            backup, size, note = self._history.keep_remote_copy(
                fs, item.remote, size=existing.size
            )
            action = Action.REPLACED_REMOTE
            target = item.remote
        else:
            if not os.path.isfile(item.local):
                return
            item.replaced = True
            backup, size, note = self._history.keep_local_copy(item.local)
            action = Action.REPLACED_LOCAL
            target = item.local
        if note:
            item.note = note
        try:
            self._history.record(
                make_entry(
                    action,
                    profile_id=self._profile_id,
                    profile_label=self._profile_label,
                    target=target,
                    backup=backup,
                    size=size,
                    note=note,
                )
            )
        except OSError as exc:
            # The journal is bookkeeping about the transfer; failing to write
            # it must not fail the transfer itself.
            item.note = f"backup kept but not journalled ({exc})"

    @staticmethod
    def _unchanged(local: str, existing) -> bool:
        """Whether the server copy already matches the file about to go up.

        Uploads carry the local timestamp over (see _preserve_mtime), so same
        size and same mtime means the last upload of this very file - the two
        seconds of slack absorb filesystems that round timestamps.
        """
        try:
            result = os.stat(local)
        except OSError:
            return False
        if existing.modified is None:
            return False
        return (
            result.st_size == existing.size
            and abs(result.st_mtime - existing.modified) <= 2.0
        )

    def _do_upload(self, fs: RemoteFS, item: TransferItem) -> None:
        progress = self._progress_for(item)
        if not self._options.atomic:
            fs.upload(item.local, item.remote, progress)
            self._verify(fs, item)
            return
        scratch = temp_name(item.remote, uuid.uuid4().hex[:8])
        try:
            fs.upload(item.local, scratch, progress)
            fs.replace(scratch, item.remote)
        except (TransferError, _Cancelled):
            try:
                fs.remove(scratch)
            except TransferError:
                pass  # Nothing was created, or it is not ours to remove.
            raise
        self._preserve_mtime(fs, item)
        self._verify(fs, item)

    def _do_download(self, fs: RemoteFS, item: TransferItem) -> None:
        progress = self._progress_for(item)
        if not self._options.atomic:
            fs.download(item.remote, item.local, progress)
            return
        scratch = f"{item.local}.mrtmp-{uuid.uuid4().hex[:8]}"
        try:
            fs.download(item.remote, scratch, progress)
            os.replace(scratch, item.local)
        except (TransferError, _Cancelled, OSError):
            try:
                os.unlink(scratch)
            except OSError:
                pass
            raise

    def _preserve_mtime(self, fs: RemoteFS, item: TransferItem) -> None:
        """Give the uploaded copy the local file's timestamp, when possible.

        Without this every deploy looks like it changed every file, which makes
        timestamp comparisons useless.
        """
        if not fs.supports(Capability.SET_MTIME):
            return
        try:
            stamp = os.path.getmtime(item.local)
        except OSError:
            return
        try:
            fs.set_mtime(item.remote, stamp)
        except TransferError:
            pass  # Cosmetic; never fail a transfer over it.

    def _verify(self, fs: RemoteFS, item: TransferItem) -> None:
        if not self._options.verify:
            return
        from mysql_runner.transfer.hashing import hash_local_file, hash_remote_file

        local_digest = hash_local_file(item.local)
        remote_digest = hash_remote_file(fs, item.remote)
        if remote_digest and local_digest != remote_digest:
            raise TransferError(
                f"{item.name} does not match after uploading - the copy on the "
                "server differs from the local file."
            )
        item.note = "verified"

    def _progress_for(self, item: TransferItem) -> ProgressCallback:
        state = {"last": 0.0}

        def report(transferred: int, total: int) -> None:
            if item._cancel or self._stopping:
                raise _Cancelled()
            while self.paused:
                if item._cancel or self._stopping:
                    raise _Cancelled()
                time.sleep(0.1)
            item.transferred = transferred
            if total and total != item.size:
                item.size = total
            now = time.monotonic()
            if now - state["last"] >= PROGRESS_INTERVAL:
                state["last"] = now
                self._events.progress(item)

        return report

    def _finish(self, item: TransferItem, state: JobState, *, error: str = "") -> None:
        with self._condition:
            item.state = state
            item.error = error
            item.finished_at = time.monotonic()
            if state == JobState.DONE and item.size:
                item.transferred = item.size
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()
            was_idle = self._active == 0 and self._queued_count() == 0
        self._events.item(item)
        if was_idle:
            self._events.idle(self.stats())


# ----- building a queue ---------------------------------------------------
def expand_local(
    fs: RemoteFS,
    sources: list[tuple[str, bool]],
    remote_base: str,
    *,
    rules: IgnoreRules | None = None,
    priority: int = PRIORITY_NORMAL,
) -> tuple[list[TransferItem], list[str], list[str]]:
    """Walk local selections into upload items.

    Returns (items, remote directories to create, skipped paths).
    """
    rules = rules or IgnoreRules.empty()
    items: list[TransferItem] = []
    directories: list[str] = []
    skipped: list[str] = []
    for path, is_dir in sources:
        name = os.path.basename(path.rstrip("\\/")) or path
        if rules.is_ignored(name, is_dir=is_dir):
            skipped.append(path)
            continue
        if not is_dir:
            items.append(
                TransferItem(
                    upload=True,
                    local=path,
                    remote=fs.join(remote_base, name),
                    size=_local_size(path),
                    priority=priority,
                )
            )
            continue
        target = fs.join(remote_base, name)
        directories.append(target)
        _walk_local_dir(fs, path, target, rules, items, directories, skipped, priority)
    return items, directories, skipped


def _walk_local_dir(
    fs: RemoteFS,
    root: str,
    remote_root: str,
    rules: IgnoreRules,
    items: list[TransferItem],
    directories: list[str],
    skipped: list[str],
    priority: int,
) -> None:
    """Plan one local directory tree into upload items."""
    for current, dirnames, filenames in os.walk(root):
        rel = local_relative(root, current)
        rel = "" if rel == "." else rel
        keep: list[str] = []
        for name in sorted(dirnames):
            child_rel = f"{rel}/{name}" if rel else name
            full = os.path.join(current, name)
            if rules.is_ignored(child_rel, is_dir=True) or os.path.islink(full):
                skipped.append(full)
                continue
            keep.append(name)
            directories.append(fs.join(remote_root, child_rel))
        dirnames[:] = keep
        for name in sorted(filenames):
            child_rel = f"{rel}/{name}" if rel else name
            full = os.path.join(current, name)
            if rules.is_ignored(child_rel):
                skipped.append(full)
                continue
            items.append(
                TransferItem(
                    upload=True,
                    local=full,
                    remote=fs.join(remote_root, child_rel),
                    size=_local_size(full),
                    priority=priority,
                )
            )


def expand_remote(
    fs: RemoteFS,
    sources: list[tuple[str, bool]],
    local_base: str,
    *,
    rules: IgnoreRules | None = None,
    priority: int = PRIORITY_NORMAL,
) -> tuple[list[TransferItem], list[str], list[str]]:
    """Walk remote selections into download items.

    Returns (items, local directories to create, skipped paths). The walk uses
    ``fs`` - the navigation connection - so the pool's own connections stay
    free for the transfers themselves.
    """
    rules = rules or IgnoreRules.empty()
    items: list[TransferItem] = []
    directories: list[str] = []
    skipped: list[str] = []
    for path, is_dir in sources:
        name = fs.basename(path)
        if rules.is_ignored(name, is_dir=is_dir):
            skipped.append(path)
            continue
        if not is_dir:
            items.append(
                TransferItem(
                    upload=False,
                    local=os.path.join(local_base, name),
                    remote=path,
                    size=_remote_size(fs, path),
                    priority=priority,
                )
            )
            continue
        target = os.path.join(local_base, name)
        directories.append(target)
        _walk_remote_dir(fs, path, target, rules, items, directories, skipped, priority)
    return items, directories, skipped


def _walk_remote_dir(
    fs: RemoteFS,
    root: str,
    local_root: str,
    rules: IgnoreRules,
    items: list[TransferItem],
    directories: list[str],
    skipped: list[str],
    priority: int,
) -> None:
    """Plan one remote directory tree into download items."""
    for current, entries in fs.walk(root):
        rel = relative_posix(root, current)
        local_dir = os.path.join(local_root, rel.replace("/", os.sep)) if rel else local_root
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if rules.is_ignored(child_rel, is_dir=entry.is_dir):
                skipped.append(fs.join(current, entry.name))
                continue
            if entry.is_dir:
                if not entry.is_link:
                    directories.append(os.path.join(local_dir, entry.name))
                continue
            items.append(
                TransferItem(
                    upload=False,
                    local=os.path.join(local_dir, entry.name),
                    remote=fs.join(current, entry.name),
                    size=entry.size,
                    priority=priority,
                )
            )


def _local_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remote_size(fs: RemoteFS, path: str) -> int:
    try:
        return fs.stat(path).size
    except TransferError:
        return 0
