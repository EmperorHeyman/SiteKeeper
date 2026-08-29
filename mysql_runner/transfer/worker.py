"""Background worker that drives a RemoteFS off the GUI thread.

Every FTP/SFTP call blocks on the network, so the file-manager tab owns one of
these on its own QThread and talks to it exclusively through signals.

Three separate channels keep the window responsive:

* the **navigation** connection - the one this object owns - handles listings
  and single operations, so browsing never waits behind anything,
* the **transfer pool** (see ``transfer/pool.py``) opens its own connections
  for the queue, and
* a **tools** connection runs the slow read-only jobs (comparisons, folder
  statistics, searches) on a plain background thread.

That is why a comparison of ten thousand files does not freeze the pane you
are looking at, and why a running queue does not stop you browsing.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from mysql_runner.storage.models import ConnectionKind
from mysql_runner.transfer.base import RemoteFS, TransferError, Unsupported
from mysql_runner.transfer.history import HistoryStore
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.pool import (
    PoolEvents,
    PoolOptions,
    TransferItem,
    TransferPool,
    expand_local,
    expand_remote,
)

#: Message used whenever an operation arrives before the connection is up.
NOT_CONNECTED = "Not connected."


class ToolCancelled(Exception):
    """Raised inside a long tool job when the user asks it to stop."""


@dataclass
class ConnectionSpec:
    """Plain-data description of a remote endpoint (safe to hand across threads)."""

    kind: ConnectionKind
    host: str
    port: int
    username: str
    password: str
    private_key_path: str = ""
    passive: bool = True

    def build(self) -> RemoteFS:
        """Instantiate the matching backend. Called on the worker thread."""
        if self.kind == ConnectionKind.SFTP:
            from mysql_runner.transfer.sftp_client import SFTPFileSystem

            return SFTPFileSystem(
                self.host,
                self.port,
                self.username,
                self.password,
                private_key_path=self.private_key_path,
            )
        from mysql_runner.transfer.ftp_client import FTPFileSystem

        return FTPFileSystem(
            self.host,
            self.port,
            self.username,
            self.password,
            use_tls=self.kind == ConnectionKind.FTPS,
            passive=self.passive,
        )

    def connected(self) -> RemoteFS:
        """A freshly connected backend, for the pool and the tool channel."""
        remote = self.build()
        remote.connect()
        return remote


@dataclass
class TransferJob:
    """One queued file copy (kept for compatibility with older callers)."""

    upload: bool
    local: str
    remote: str

    @property
    def name(self) -> str:
        return os.path.basename(self.local) or self.remote


class TransferWorker(QObject):
    """Runs remote filesystem operations off the GUI thread."""

    #: Connection established, with a banner for the status line.
    connected = pyqtSignal(str)
    #: What this connection turned out to be able to do (frozenset[Capability]).
    capabilities_ready = pyqtSignal(object)
    #: Connection could not be established (fatal for the tab).
    failed = pyqtSignal(str)
    #: A directory listing completed: (path, list[RemoteEntry]).
    listing = pyqtSignal(str, object)
    #: Internal: re-list a directory once the queue has drained. The pool
    #: signals idle from one of its *own* threads, which must not be the
    #: thread that talks on the navigation connection - hence a signal
    #: rather than a call. See _on_pool_idle.
    _refresh_listing = pyqtSignal(str)
    #: A single operation failed (non-fatal).
    op_failed = pyqtSignal(str)
    #: An operation succeeded, with a message for the status line.
    op_done = pyqtSignal(str)
    #: A queue is about to run: (number of files, what triggered it).
    #: The trigger is a short key the queue panel turns into a label, so
    #: a batch that appeared on its own can say why.
    queue_started = pyqtSignal(int, str)
    #: Transfer progress: (filename, transferred_bytes, total_bytes).
    progress = pyqtSignal(str, int, int)
    #: One file of the queue finished copying.
    file_finished = pyqtSignal(str)
    #: The whole queue finished: (completed_count, failed_count, cancelled).
    queue_finished = pyqtSignal(int, int, bool)
    #: One queue entry changed state (a TransferItem snapshot).
    queue_item = pyqtSignal(object)
    #: Queue totals, for the panel header.
    queue_stats = pyqtSignal(object)
    #: A remote file fetched for local editing is ready: (local, remote).
    edit_ready = pyqtSignal(str, str)
    #: A tool job finished: (kind, payload).
    tool_result = pyqtSignal(str, object)
    #: A tool job failed: (kind, message).
    tool_failed = pyqtSignal(str, str)
    #: Progress of a long tool job: (kind, text).
    tool_progress = pyqtSignal(str, str)
    #: Sub-folders of one directory, for the folder picker: (path, list[str]).
    folders_listed = pyqtSignal(str, object)
    #: The folder picker could not read a directory: (path, message).
    folders_failed = pyqtSignal(str, str)
    #: The connection has been closed.
    closed = pyqtSignal()

    def __init__(
        self,
        *,
        options: PoolOptions | None = None,
        history: HistoryStore | None = None,
        profile_id: str = "",
        profile_label: str = "",
    ) -> None:
        super().__init__()
        self._fs: RemoteFS | None = None
        self._spec: ConnectionSpec | None = None
        self._cancelled = False
        self._options = (options or PoolOptions()).sane()
        self._history = history
        self._profile_id = profile_id
        self._profile_label = profile_label
        self._pool: TransferPool | None = None
        self._tool_fs: RemoteFS | None = None
        self._tool_lock = threading.Lock()
        self._tool_cancel = False
        self._tool_busy = ""
        self._browse_fs: RemoteFS | None = None
        self._browse_lock = threading.Lock()
        self._queue_totals = [0, 0, 0]  # queued, completed, failed
        self._last_listing = ""        # refreshed after a queue drains
        # Queued explicitly: the emit comes from a pool thread and the
        # listing has to happen on the thread this object lives on.
        self._refresh_listing.connect(
            self.list_dir, Qt.ConnectionType.QueuedConnection
        )

    # ----- cancellation ---------------------------------------------------
    def cancel(self) -> None:
        """Ask the running queue to stop. Safe to call from the GUI thread."""
        self._cancelled = True
        if self._pool is not None:
            self._pool.cancel_all()

    def cancel_tools(self) -> None:
        """Ask a long comparison or search to give up."""
        self._tool_cancel = True

    # ----- lifecycle ------------------------------------------------------
    @pyqtSlot(object)
    def open_connection(self, spec: object) -> None:
        assert isinstance(spec, ConnectionSpec)
        try:
            fs = spec.build()
            banner = fs.connect()
        except TransferError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # backend import errors, unexpected failures
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self._fs = fs
        self._spec = spec
        self.connected.emit(banner)
        self.capabilities_ready.emit(fs.capabilities())

    @pyqtSlot()
    def close_connection(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        with self._tool_lock:
            if self._tool_fs is not None:
                try:
                    self._tool_fs.close()
                except Exception:
                    pass
                self._tool_fs = None
        with self._browse_lock:
            if self._browse_fs is not None:
                try:
                    self._browse_fs.close()
                except Exception:
                    pass
                self._browse_fs = None
        if self._fs is not None:
            self._fs.close()
            self._fs = None
        self.closed.emit()

    @pyqtSlot()
    def request_home(self) -> None:
        if self._fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            home = self._with_session(lambda fs: fs.home())
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.list_dir(home)

    # ----- the navigation session ------------------------------------------
    # Idle connections get cut by servers and firewalls without a word.
    # Without the revival below, the first click after a pause failed, every
    # click after it failed the same way, and the only way out was closing
    # the tab.
    def _revive(self) -> RemoteFS:
        """Replace a dead navigation session with a fresh one (or raise)."""
        fs, self._fs = self._fs, None
        if fs is not None:
            try:
                fs.close()
            except Exception:
                pass
        spec = self._spec
        if spec is None:
            raise TransferError(NOT_CONNECTED)
        self.op_done.emit("The connection was dropped - reconnecting…")
        fresh = spec.build()
        fresh.connect()  # a TransferError here reaches the caller as-is
        self._fs = fresh
        self.op_done.emit("Reconnected.")
        return fresh

    def _with_session(self, operation):
        """Run ``operation(fs)``, reviving the session when it has died."""
        fs = self._fs
        if fs is None:
            raise TransferError(NOT_CONNECTED)
        try:
            return operation(fs)
        except TransferError:
            if self._spec is None or fs.alive():
                raise  # the session is fine; the operation itself was refused
            return operation(self._revive())

    def _ensure_session(self) -> RemoteFS | None:
        """The navigation session, probed and revived before a queue starts.

        Queue starts walk trees and create directories on this connection
        before the pool takes over, so a dead session must be noticed now,
        not as a wall of failures afterwards. One NOOP per batch is cheap.
        """
        fs = self._fs
        if fs is None or self._spec is None or fs.alive():
            return fs
        try:
            return self._revive()
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return None
        except Exception as exc:
            self.op_failed.emit(str(exc) or exc.__class__.__name__)
            return None

    # ----- operations -----------------------------------------------------
    @pyqtSlot(str)
    def list_dir(self, path: str) -> None:
        if self._fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            entries = self._with_session(lambda fs: fs.listdir(path))
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self._last_listing = path
        self.listing.emit(path, entries)

    @pyqtSlot(str)
    def make_dir(self, path: str) -> None:
        """Create a folder, including any parents it needs.

        makedirs rather than mkdir: typing a path into the New folder box -
        ``releases/2026/08`` - is the obvious thing to do and used to fail
        with whatever the server says about a missing parent, which reads
        like the whole feature is broken rather than like a hint to make
        three folders one at a time.
        """
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            self._with_session(lambda fs: fs.makedirs(path))
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Created {path}")
        self.list_dir(fs.parent(path))

    @pyqtSlot(str)
    def make_file(self, path: str) -> None:
        """Create an empty file on the server.

        There is no "touch" in either protocol, so an empty local file is
        uploaded instead - which also makes the parent folders on the way,
        the same as New folder does.

        An existing file is never overwritten. Creating one is not the same
        request as emptying one, and a New file box that silently truncated
        a config nobody meant to touch would be a genuinely dangerous way to
        find that out.
        """
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            if self._with_session(lambda fs: fs.exists(path)):
                self.op_failed.emit(
                    f"{fs.basename(path)} is already there; it was left alone."
                )
                return
            parent = fs.parent(path)
            if parent:
                self._with_session(lambda fs: fs.makedirs(parent))
            blank = os.path.join(tempfile.gettempdir(), "sitekeeper-new-file")
            with open(blank, "wb"):
                pass
            self._with_session(lambda fs: fs.upload(blank, path))
        except (TransferError, OSError) as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Created {path}")
        self.list_dir(fs.parent(path))

    @pyqtSlot(str, bool)
    def delete_entry(self, path: str, is_dir: bool) -> None:
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            if is_dir:
                self._with_session(lambda fs: self._delete_tree(fs, path))
            else:
                self._with_session(lambda fs: fs.remove(path))
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Deleted {fs.basename(path)}")
        self.list_dir(fs.parent(path))

    def _delete_tree(self, fs: RemoteFS, path: str) -> None:
        """Remove a directory and everything in it.

        rmdir only takes empty directories, so a folder with contents used to
        fail with a bare "directory not empty". Children go first, deepest
        last, and a symlinked directory is unlinked rather than followed.
        """
        try:
            entries = fs.listdir(path)
        except TransferError:
            fs.rmdir(path)
            return
        for entry in entries:
            child = fs.join(path, entry.name)
            if entry.is_dir and not entry.is_link:
                self._delete_tree(fs, child)
            else:
                fs.remove(child)
        fs.rmdir(path)

    @pyqtSlot(str, str)
    def rename_entry(self, source: str, target: str) -> None:
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            self._with_session(lambda fs: fs.rename(source, target))
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Renamed to {fs.basename(target)}")
        self.list_dir(fs.parent(target))

    @pyqtSlot(str, int, bool, str)
    def request_chmod(self, path: str, mode: int, recursive: bool, scope: str) -> None:
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            if recursive:
                from mysql_runner.transfer.remote_exec import chmod_tree

                chmod_tree(fs, path, mode, scope=scope)
            else:
                fs.chmod(path, mode)
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        from mysql_runner.transfer.permissions import to_octal

        suffix = " (recursively)" if recursive else ""
        self.op_done.emit(f"{fs.basename(path)} is now {to_octal(mode)}{suffix}")
        self.list_dir(fs.parent(path))

    @pyqtSlot(str, str)
    def request_symlink(self, target: str, link_path: str) -> None:
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            if fs.exists(link_path):
                fs.remove(link_path)  # Retargeting means replacing the link.
            fs.symlink(target, link_path)
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"{fs.basename(link_path)} now points at {target}")
        self.list_dir(fs.parent(link_path))

    @pyqtSlot(str, str, str, str)
    def request_archive(self, directory: str, names: str, archive: str, kind: str) -> None:
        """Pack the given names (newline-separated) on the server."""
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        from mysql_runner.transfer.remote_exec import make_archive

        wanted = [name for name in names.split("\n") if name]
        try:
            make_archive(fs, directory, wanted, archive, kind=kind)
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Created {fs.basename(archive)} on the server")
        self.list_dir(directory)

    @pyqtSlot(str, str)
    def request_extract(self, archive: str, destination: str) -> None:
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        from mysql_runner.transfer.remote_exec import extract_archive

        try:
            extract_archive(fs, archive, destination)
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(f"Unpacked {fs.basename(archive)}")
        self.list_dir(destination)

    @pyqtSlot(str, str)
    def request_exec(self, command: str, cwd: str) -> None:
        """Run one command and hand the whole result back."""
        fs = self._fs
        if fs is None:
            self.tool_failed.emit("exec", NOT_CONNECTED)
            return
        from mysql_runner.transfer.remote_exec import run

        try:
            result = run(fs, command, cwd=cwd, timeout=120)
        except TransferError as exc:
            self.tool_failed.emit("exec", str(exc))
            return
        self.tool_result.emit("exec", result)

    @pyqtSlot(str, str)
    def fetch_for_edit(self, remote: str, local: str) -> None:
        """Download one file so it can be edited locally.

        Runs on the navigation connection - an edit begins from a click in the
        listing, so the pane is idle at that moment anyway - and the tab keeps
        watching the local copy afterwards to upload each save.
        """
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        try:
            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            self._with_session(lambda fs: fs.download(remote, local))
        except (TransferError, OSError) as exc:
            self.op_failed.emit(f"{fs.basename(remote)}: {exc}")
            return
        self.edit_ready.emit(local, remote)

    @pyqtSlot(str)
    def request_undo(self, entry_id: str) -> None:
        if self._history is None:
            self.op_failed.emit("Shadow backups are switched off.")
            return
        fs = self._fs
        try:
            message = self._history.undo(entry_id, fs)
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            return
        self.op_done.emit(message)
        if fs is not None and self._last_listing:
            self.list_dir(self._last_listing)

    # ----- the transfer queue --------------------------------------------
    def _ensure_pool(self) -> TransferPool | None:
        if self._pool is not None:
            return self._pool
        spec = self._spec
        if spec is None:
            return None
        events = PoolEvents(
            on_item=self._on_pool_item,
            on_progress=self._on_pool_progress,
            on_message=self.op_done.emit,
            on_idle=self._on_pool_idle,
        )
        self._pool = TransferPool(
            spec.connected,
            options=self._options,
            events=events,
            history=self._history,
            profile_id=self._profile_id,
            profile_label=self._profile_label,
        )
        return self._pool

    def _on_pool_item(self, item) -> None:
        from mysql_runner.transfer.pool import JobState

        self.queue_item.emit(item)
        if item.state == JobState.DONE:
            self._queue_totals[1] += 1
            self.file_finished.emit(item.name)
        elif item.state == JobState.FAILED:
            self._queue_totals[2] += 1
            self.op_failed.emit(f"{item.name}: {item.error}")
        elif item.state == JobState.SKIPPED and item.note:
            self.op_done.emit(f"{item.name}: {item.note}")

    def _on_pool_progress(self, item) -> None:
        self.progress.emit(item.name, item.transferred, item.size)

    def _on_pool_idle(self, stats: dict) -> None:
        """The queue drained. Runs on whichever pool thread finished last.

        Everything here must therefore be thread-safe, and one thing here
        very much was not: the refresh below used to be a direct call, so a
        pool thread ran a listing on the *navigation* connection while the
        worker thread was quite possibly using it too. Two threads on one
        FTP control socket - or one paramiko SFTP channel - interleave their
        requests and the session never recovers: replies are read by the
        wrong caller, and the next operation blocks on a response that is
        never coming. paramiko sets no read timeout, so 'blocks' means
        forever, and the worker thread stops returning to its event loop.
        Every later upload, listing or drop then sat in the queue unread -
        no error, no queue entry, nothing happening at all, until the tab
        was reopened.

        A commit-driven sync is what made it likely: it submits one batch
        per sub-directory, so the pool falls idle again and again while the
        worker thread is still working through the next batch - or, with
        delete_remote on, still walking a tree of removals on that same
        connection. Signals cross threads safely; calls do not.
        """
        self.queue_stats.emit(stats)
        counts = stats.get("counts", {})
        cancelled = bool(counts.get("cancelled"))
        self.queue_finished.emit(
            self._queue_totals[1], self._queue_totals[2], cancelled
        )
        self._queue_totals = [0, 0, 0]
        # Refresh whatever directory the transfers landed in - on the
        # worker thread, once it is free.
        if self._last_listing:
            self._refresh_listing.emit(self._last_listing)

    @pyqtSlot(object, str)
    def run_download(self, items: object, local_dir: str) -> None:
        """Download files and folders into ``local_dir``.

        ``items`` is a list of (remote_path, is_dir) pairs, optionally followed
        by an IgnoreRules instance. Directories are walked here, on the
        navigation connection, because listing them needs a live session.
        """
        fs = self._ensure_session()
        pool = self._ensure_pool()
        if fs is None or pool is None:
            self._fail_queue()
            return
        sources, rules, origin = _split_items(items)
        self._cancelled = False
        try:
            jobs, directories, skipped = expand_remote(
                fs, sources, local_dir, rules=rules
            )
        except TransferError as exc:
            self.op_failed.emit(str(exc))
            self.queue_finished.emit(0, 1, False)
            return
        for path in directories:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as exc:
                self.op_failed.emit(f"{path}: {exc}")
        self._start_queue(pool, jobs, skipped, origin)

    @pyqtSlot(object)
    def download_groups(self, payload: object) -> None:
        """Fetch several directories' worth of files as one queue.

        The mirror of upload_groups, and it had the same problem: a tree
        pulled down was handed over one destination folder at a time, so
        each became its own queue and the pool drained between every one.

        ``payload`` is (groups, rules, origin), where groups is a list of
        (local directory, [(remote path, is_dir), ...]).
        """
        fs = self._ensure_session()
        pool = self._ensure_pool()
        if fs is None or pool is None:
            self._fail_queue()
            return
        groups, rules, origin = _split_groups(payload)
        self._cancelled = False
        jobs: list[TransferItem] = []
        skipped: list[str] = []
        made: set[str] = set()
        for target, sources in groups:
            try:
                batch, directories, missed = expand_remote(
                    fs, sources, target, rules=rules
                )
            except TransferError as exc:
                # One unreadable folder must not take the rest of the pull
                # down with it, the way a single queue per group used to.
                self.op_failed.emit(str(exc))
                continue
            jobs.extend(batch)
            skipped.extend(missed)
            for directory in [target, *directories]:
                if not directory or directory in made:
                    continue
                made.add(directory)
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError as exc:
                    self.op_failed.emit(f"{directory}: {exc}")
        self._start_queue(pool, jobs, skipped, origin)

    @pyqtSlot(object, str)
    def run_upload(self, items: object, remote_dir: str) -> None:
        """Upload files and folders into ``remote_dir``."""
        fs = self._ensure_session()
        pool = self._ensure_pool()
        if fs is None or pool is None:
            self._fail_queue()
            return
        sources, rules, origin = _split_items(items)
        self._cancelled = False
        jobs, directories, skipped = expand_local(fs, sources, remote_dir, rules=rules)
        if remote_dir and remote_dir != self._last_listing:
            # A sync can aim single files at a directory that does not exist
            # yet (a commit that adds a new folder); the directory on show
            # obviously exists, so it is not paid for.
            try:
                fs.makedirs(remote_dir)
            except TransferError:
                pass  # a real problem surfaces when the first file lands
        for path in directories:
            try:
                fs.mkdir(path)
            except TransferError:
                # Almost always "already exists", which is fine here. A real
                # permission problem surfaces when the first file lands.
                pass
        self._last_listing = remote_dir
        self._start_queue(pool, jobs, skipped, origin)

    @pyqtSlot(object, str, bool)
    def upload_quietly(
        self, items: object, remote_dir: str, create: bool = False
    ) -> None:
        """Upload without adopting ``remote_dir`` as the directory on show.

        Edit-in-place saves land wherever the edited file lives, which is not
        necessarily where the user is browsing - the refresh after the queue
        drains must not yank the remote pane over there. Everything a trigger
        starts - a save, a commit, a comparison - comes this way for the same
        reason: it was never the user asking to go anywhere.

        ``create`` makes the target directory first, for the callers whose
        target may not be there yet - a commit that adds a folder. A save of a
        file edited in place is not one of them: it came from that directory,
        so the round trips would buy nothing.
        """
        fs = self._ensure_session()
        pool = self._ensure_pool()
        if fs is None or pool is None:
            self._fail_queue()
            return
        sources, rules, origin = _split_items(items)
        self._cancelled = False
        jobs, directories, skipped = expand_local(fs, sources, remote_dir, rules=rules)
        if create and remote_dir and remote_dir != self._last_listing:
            # As in run_upload: a sync can aim single files at a directory that
            # does not exist yet, and a commit that adds a folder does exactly
            # that. The directory on show obviously exists, so it is not paid
            # for.
            try:
                fs.makedirs(remote_dir)
            except TransferError:
                pass  # a real problem surfaces when the first file lands
        for path in directories:
            try:
                fs.mkdir(path)
            except TransferError:
                pass
        self._start_queue(pool, jobs, skipped, origin)

    @pyqtSlot(object, str, bool)
    def upload_groups(self, payload: object, remote_dir: str, quiet: bool) -> None:
        """Send several sub-directories' worth of files as one queue.

        A tree push is grouped by sub-directory, because each group lands
        somewhere different. It used to be handed over one group at a time,
        which meant one *queue* each: the pool drained and restarted between
        them, so the panel's batch counter reset over and over, the queue's
        history filled with a dozen batches nobody asked for, and every one
        of those drains re-listed the directory on show and made the tab
        reload its local pane. A sync of a site with twenty folders paid all
        of that twenty times. One queue instead: the pool falls idle once,
        at the end, which is the only moment any of it is worth doing.

        ``payload`` is (groups, rules, origin), where groups is a list of
        (remote directory, [(local path, is_dir), ...]).

        ``quiet`` leaves the remote pane where it is. Anything a trigger
        started - a save, a commit, a comparison - has to be quiet: nobody
        asked to be taken anywhere.
        """
        fs = self._ensure_session()
        pool = self._ensure_pool()
        if fs is None or pool is None:
            self._fail_queue()
            return
        groups, rules, origin = _split_groups(payload)
        self._cancelled = False
        jobs: list[TransferItem] = []
        skipped: list[str] = []
        # One attempt per directory across the whole push. Sibling groups
        # share every parent above them, and makedirs walks that chain.
        made: set[str] = set()
        for target, sources in groups:
            batch, directories, missed = expand_local(
                fs, sources, target, rules=rules
            )
            jobs.extend(batch)
            skipped.extend(missed)
            # A sync can aim files at a directory that does not exist yet -
            # a commit that adds a folder. The directory on show obviously
            # exists, so it is not paid for.
            if target and target not in made and target != self._last_listing:
                made.add(target)
                try:
                    fs.makedirs(target)
                except TransferError:
                    pass  # a real problem surfaces when the first file lands
            for path in directories:
                if path in made:
                    continue
                made.add(path)
                try:
                    fs.mkdir(path)
                except TransferError:
                    pass  # almost always "already exists"
        if not quiet and remote_dir:
            # The base of the push, not whichever sub-directory happened to
            # go up last: that used to leave the pane inside an arbitrary
            # folder, and the next push aimed at /admin/admin/file.
            self._last_listing = remote_dir
        self._start_queue(pool, jobs, skipped, origin)

    def _start_queue(
        self,
        pool: TransferPool,
        jobs: list[TransferItem],
        skipped: list[str],
        origin: str = "",
    ) -> None:
        if self._cancelled:
            # Cancel was pressed while the tree was still being walked. The
            # queue does not exist yet, so there is nothing for the pool to
            # stop - it must simply never be submitted.
            self.queue_started.emit(0, origin)
            self.queue_finished.emit(0, 0, True)
            self._cancelled = False
            return
        self._queue_totals = [len(jobs), 0, 0]
        self.queue_started.emit(len(jobs), origin)
        if not jobs:
            # Order matters: the status line keeps the last thing said, and
            # queue_finished says "0 file(s) transferred", which is true and
            # useless. It used to land *after* the reason and bury it, so a
            # push that was filtered out entirely looked like nothing had
            # happened at all. The reason goes last, and names names.
            self.queue_finished.emit(0, 0, False)
            if skipped:
                self.op_failed.emit(_skip_reason(skipped))
            return
        if skipped:
            self.op_done.emit(_skip_reason(skipped))
        # A new run starts from a clean slate: what the last one finished stays
        # visible in the panel's history, but the pool - and so the counters -
        # should speak about the work at hand, not everything since connecting.
        # Failures stay, so "Retry failed" still has something to retry.
        pool.clear_finished(keep_failed=True)
        pool.submit(jobs)
        self.queue_stats.emit(pool.stats())

    def _fail_queue(self) -> None:
        self.op_failed.emit(NOT_CONNECTED)
        self.queue_finished.emit(0, 0, False)

    @pyqtSlot()
    def pause_queue(self) -> None:
        if self._pool is not None:
            self._pool.pause()
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot()
    def resume_queue(self) -> None:
        if self._pool is not None:
            self._pool.resume()
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot(str)
    def cancel_item(self, item_id: str) -> None:
        if self._pool is not None and self._pool.cancel(item_id):
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot(str)
    def prioritize_item(self, item_id: str) -> None:
        if self._pool is not None:
            self._pool.prioritize(item_id)

    @pyqtSlot(object)
    def reorder_queue(self, item_ids: object) -> None:
        if self._pool is not None and isinstance(item_ids, list):
            self._pool.reorder(item_ids)

    @pyqtSlot()
    def clear_finished(self) -> None:
        if self._pool is not None:
            self._pool.clear_finished()
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot()
    def retry_failed(self) -> None:
        if self._pool is None:
            return
        count = self._pool.retry_failed()
        if count:
            self._queue_totals[0] += count
            self.op_done.emit(f"Retrying {count} failed transfer(s).")
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot(str)
    def retry_item(self, item_id: str) -> None:
        if self._pool is not None and self._pool.retry(item_id):
            self._queue_totals[0] += 1
            self.queue_stats.emit(self._pool.stats())

    @pyqtSlot(int)
    def set_workers(self, count: int) -> None:
        self._options.workers = max(1, count)
        if self._pool is not None:
            self._pool.set_workers(count)

    @pyqtSlot(object)
    def update_options(self, options: object) -> None:
        """Apply changed transfer settings without reconnecting."""
        if not isinstance(options, PoolOptions):
            return
        self._options = options.sane()
        if self._pool is not None:
            self._pool.set_workers(self._options.workers)

    # ----- the folder picker's channel ------------------------------------
    # Deliberately neither the navigation session nor the tool channel. The
    # navigation session's listings drive the remote pane, so borrowing it
    # would yank the pane around while somebody is only looking for a folder;
    # the tool channel takes one job at a time and a folder-size sweep can sit
    # on it for a minute, which is far too long to wait for a directory list.
    def _browse_connection(self) -> RemoteFS:
        """The picker's own read-only session. Call with ``_browse_lock`` held."""
        if self._browse_fs is not None:
            if self._browse_fs.alive():
                return self._browse_fs
            try:
                self._browse_fs.close()
            except Exception:
                pass
            self._browse_fs = None
        spec = self._spec
        if spec is None:
            raise TransferError(NOT_CONNECTED)
        self._browse_fs = spec.connected()
        return self._browse_fs

    @pyqtSlot(str)
    def request_folders(self, path: str) -> None:
        """Name the directories inside ``path`` for the remote folder picker.

        Files are dropped here rather than in the dialog: a listing of a
        release directory can be thousands of entries, and none of them are
        anything the picker can show.
        """
        target = path or "/"

        def runner() -> None:
            with self._browse_lock:
                try:
                    entries = self._browse_connection().listdir(target)
                except (TransferError, OSError) as exc:
                    self.folders_failed.emit(target, str(exc))
                    return
                except Exception as exc:
                    self.folders_failed.emit(
                        target, str(exc) or exc.__class__.__name__
                    )
                    return
            names = sorted(
                (entry.name for entry in entries if entry.is_dir),
                key=str.lower,
            )
            self.folders_listed.emit(target, names)

        threading.Thread(target=runner, name="browse-folders", daemon=True).start()

    # ----- tool jobs (their own connection, their own thread) -------------
    def _tool_connection(self) -> RemoteFS:
        """The read-only connection the slow jobs use.

        It sits idle between jobs, which is exactly how connections get
        dropped - so it is probed before being trusted and reopened when dead.
        """
        with self._tool_lock:
            if self._tool_fs is not None:
                if self._tool_fs.alive():
                    return self._tool_fs
                try:
                    self._tool_fs.close()
                except Exception:
                    pass
                self._tool_fs = None
            spec = self._spec
            if spec is None:
                raise TransferError(NOT_CONNECTED)
            self._tool_fs = spec.connected()
            return self._tool_fs

    def _run_tool(self, kind: str, job) -> None:
        """Run ``job(fs)`` on a background thread and report the result."""
        if self._tool_busy:
            self.tool_failed.emit(
                kind, f"Still busy with the previous {self._tool_busy} job."
            )
            return
        self._tool_busy = kind
        self._tool_cancel = False

        def runner() -> None:
            try:
                fs = self._tool_connection()
                payload = job(fs)
            except ToolCancelled:
                self.tool_failed.emit(kind, "Cancelled.")
            except Unsupported as exc:
                self.tool_failed.emit(kind, str(exc))
            except TransferError as exc:
                self.tool_failed.emit(kind, str(exc))
            except OSError as exc:
                self.tool_failed.emit(kind, str(exc))
            else:
                self.tool_result.emit(kind, payload)
            finally:
                self._tool_busy = ""

        threading.Thread(target=runner, name=f"tool-{kind}", daemon=True).start()

    def _tool_ticker(self, kind: str, label: str):
        """A progress callback that also honours a cancel request."""

        def report(*args) -> None:
            if self._tool_cancel:
                raise ToolCancelled()
            if args:
                self.tool_progress.emit(kind, f"{label}: {args[0]}")

        return report

    @pyqtSlot(str, object)
    def request_folder_stats(self, parent: str, names: object) -> None:
        """Recursive size and newest-content date for the folders in a listing."""
        assert isinstance(names, list)

        def job(fs: RemoteFS) -> dict:
            from mysql_runner.transfer.treestat import remote_folder_stats

            out: dict[str, object] = {}
            for name in names:
                if self._tool_cancel:
                    break
                try:
                    out[name] = remote_folder_stats(fs, fs.join(parent, name))
                except TransferError:
                    continue
            return {"parent": parent, "stats": out}

        self._run_tool("folder_stats", job)

    @pyqtSlot(str, str, bool, object)
    def request_compare(
        self, local_dir: str, remote_dir: str, with_hashes: bool, rules: object
    ) -> None:
        """Compare both sides and report which files differ."""
        ignore = rules if isinstance(rules, IgnoreRules) else IgnoreRules.empty()

        def job(fs: RemoteFS):
            from mysql_runner.transfer.hashing import (
                compare,
                snapshot_local,
                snapshot_remote,
            )

            ticker = self._tool_ticker("compare", "Reading local files")
            local = snapshot_local(
                local_dir, rules=ignore, with_hashes=with_hashes, on_progress=ticker
            )
            remote_ticker = self._tool_ticker("compare", "Reading the server")
            remote = snapshot_remote(
                fs, remote_dir, rules=ignore, with_hashes=with_hashes,
                on_progress=remote_ticker,
            )
            report = compare(local, remote)
            return {
                "local_dir": local_dir,
                "remote_dir": remote_dir,
                "report": report,
            }

        self._run_tool("compare", job)

    @pyqtSlot(str, str, bool, object, str, bool)
    def request_sync_scan(
        self,
        local_dir: str,
        remote_dir: str,
        with_hashes: bool,
        rules: object,
        rule_id: str,
        recursive: bool = True,
    ) -> None:
        """Compare a synced folder with the server, for an automatic sync.

        The same work as :meth:`request_compare`, reported under its own tool
        kind and tagged with the rule that asked: a sync running in the
        background must not take over the Compare window the user opened, and
        the tab has to know which of several synced folders came back.
        """
        ignore = rules if isinstance(rules, IgnoreRules) else IgnoreRules.empty()

        def job(fs: RemoteFS):
            from mysql_runner.transfer.hashing import (
                compare,
                snapshot_local,
                snapshot_remote,
            )

            ticker = self._tool_ticker("sync_scan", "Reading local files")
            local = snapshot_local(
                local_dir, rules=ignore, with_hashes=with_hashes,
                on_progress=ticker, recursive=recursive,
            )
            remote_ticker = self._tool_ticker("sync_scan", "Reading the server")
            remote = snapshot_remote(
                fs, remote_dir, rules=ignore, with_hashes=with_hashes,
                on_progress=remote_ticker, recursive=recursive,
            )
            return {
                "rule_id": rule_id,
                "local_dir": local_dir,
                "remote_dir": remote_dir,
                "report": compare(local, remote),
            }

        self._run_tool("sync_scan", job)

    @pyqtSlot(object)
    def delete_quietly(self, paths: object) -> None:
        """Delete remote paths without moving the pane the user is looking at.

        :meth:`delete_entry` re-lists the parent of what it deleted, which is
        right for a deliberate delete and wrong for a background sync - it would
        yank the remote pane off to wherever the deleted file happened to live.
        This deletes the lot, then refreshes only the directory already on show.
        """
        if not isinstance(paths, list) or not paths:
            return
        fs = self._ensure_session()
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        removed = 0
        failed: list[str] = []
        for path in paths:
            try:
                stat = fs.stat(path)
            except TransferError:
                continue  # already gone: the sync has nothing to do
            try:
                if stat.is_dir and not stat.is_link:
                    self._delete_tree(fs, path)
                else:
                    fs.remove(path)
            except TransferError as exc:
                failed.append(f"{fs.basename(path)}: {exc}")
                continue
            removed += 1
        if removed:
            self.op_done.emit(f"Removed {removed} file(s) on the server")
        for message in failed[:3]:
            self.op_failed.emit(message)
        showing = self._last_listing
        if removed and showing:
            try:
                self.list_dir(showing)
            except TransferError:
                pass

    @pyqtSlot(str)
    def request_digest(self, remote_path: str) -> None:
        def job(fs: RemoteFS):
            from mysql_runner.transfer.hashing import hash_remote_file

            return {"path": remote_path, "digest": hash_remote_file(fs, remote_path)}

        self._run_tool("digest", job)

    @pyqtSlot(str)
    def request_disk_usage(self, path: str) -> None:
        def job(fs: RemoteFS):
            from mysql_runner.transfer.remote_exec import disk_usage

            return disk_usage(fs, path)

        self._run_tool("disk_usage", job)

    @pyqtSlot(str, str, bool, bool, str)
    def request_grep(
        self, root: str, pattern: str, fixed: bool, ignore_case: bool, include: str
    ) -> None:
        def job(fs: RemoteFS):
            from mysql_runner.transfer.remote_exec import grep

            return grep(
                fs, root, pattern, fixed=fixed, ignore_case=ignore_case, include=include
            )

        self._run_tool("grep", job)

    @pyqtSlot(str)
    def request_logs(self, directory: str) -> None:
        def job(fs: RemoteFS):
            from mysql_runner.transfer.remote_exec import list_logs

            return list_logs(fs, directory)

        self._run_tool("logs", job)

    @pyqtSlot(object, str)
    def upload_paths(self, paths: object, remote_dir: str) -> None:
        """Upload a list of local files (used by the directory watcher)."""
        assert isinstance(paths, list)
        items = [(path, os.path.isdir(path)) for path in paths if os.path.exists(path)]
        if items:
            self.run_upload(items, remote_dir)


def _skip_reason(skipped: list[str]) -> str:
    """Say which files the ignore rules held back, by name.

    A count on its own ("3 item(s) skipped") does not let anyone work out
    which three, and for the single-file case - the one that actually
    happens, someone sending .env - it is no help whatsoever.
    """
    names = [os.path.basename(path.rstrip("\\/")) or path for path in skipped]
    shown = ", ".join(names[:4])
    if len(names) > 4:
        shown += f", and {len(names) - 4} more"
    subject = "it matches" if len(names) == 1 else "they match"
    return f"Not sent: {shown} - {subject} the ignore rules."


def _split_groups(
    payload: object,
) -> tuple[list[tuple[str, list[tuple[str, bool]]]], IgnoreRules | None, str]:
    """Unpack (groups, rules, origin) for upload_groups.

    ``groups`` arrives as (remote directory, sources) pairs; the rest is
    read exactly as _split_items reads it.
    """
    assert isinstance(payload, tuple) and len(payload) == 3
    groups, rules, origin = payload
    return (
        [(str(target), list(sources)) for target, sources in groups],
        rules if isinstance(rules, IgnoreRules) else None,
        str(origin),
    )


def _split_items(
    items: object,
) -> tuple[list[tuple[str, bool]], IgnoreRules | None, str]:
    """Accept a plain list of sources, (sources, rules), or (sources, rules, origin).

    ``origin`` is the short key naming whatever started the transfer ("git",
    "save", ...). It rides along with the sources rather than as a slot
    argument so that adding it did not have to change every signal in the tab.
    """
    if isinstance(items, tuple) and len(items) in (2, 3):
        sources, rules = items[0], items[1]
        origin = str(items[2]) if len(items) == 3 else ""
        return (
            list(sources),
            rules if isinstance(rules, IgnoreRules) else None,
            origin,
        )
    assert isinstance(items, list)
    return list(items), None, ""
