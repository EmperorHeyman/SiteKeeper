"""Background worker that drives a RemoteFS off the GUI thread.

Every FTP/SFTP call blocks on the network, so the file-manager tab owns one of
these on its own QThread and talks to it exclusively through signals.

Four separate channels keep the window responsive:

* the **navigation** connection - the one this object owns - handles listings
  and the operations that finish in a round trip or two, so browsing never
  waits behind anything,
* the **transfer pool** (see ``transfer/pool.py``) opens its own connections
  for the queue,
* a **tools** connection runs the slow read-only jobs (comparisons, folder
  statistics, searches) on a plain background thread, and
* an **operations** connection runs the slow *writing* jobs - deleting a
  tree, a recursive chmod, fetching a file to edit - one after another.

The last of those is the newest and was the last thing still able to lock the
window up. Deleting a folder is not one operation, it is one per file in it
and one per directory, strictly in order because a directory cannot go until
it is empty; on the navigation connection that meant the pane you were looking
at stopped answering for as long as the tree took. Nothing that walks a tree
belongs on the connection the panes browse with.

That is why a comparison of ten thousand files does not freeze the pane you
are looking at, and why a running queue does not stop you browsing.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from mysql_runner.storage.models import ConnectionKind
from mysql_runner.transfer.base import RemoteFS, TransferError, Unsupported
from mysql_runner.transfer import hostkeys
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
from mysql_runner.transfer.removal import delete_paths

#: Message used whenever an operation arrives before the connection is up.
NOT_CONNECTED = "Not connected."

#: Shortest gap between two status-line messages from one long operation.
OPS_PROGRESS_INTERVAL = 0.3

#: Shortest gap between two queue-stats updates while transfers are running.
#: The panel's rate and time-remaining want refreshing often enough to look
#: live and rarely enough that measuring them is free.
STATS_INTERVAL = 0.4


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
    #: SSH authentication beyond a password or a named key file.
    use_agent: bool = True
    use_default_keys: bool = False
    #: What to do about a host key nobody has seen before. The application
    #: asks; headless callers, which build their backends directly rather
    #: than through a spec, record it and carry on.
    host_key_mode: str = hostkeys.PROMPT
    #: The bastion to reach this server through, already resolved from
    #: whichever profile was named - a spec has to be plain data.
    jump: object = None
    proxy_command: str = ""

    @classmethod
    def for_profile(cls, profile, jump: object = None) -> "ConnectionSpec":
        """Everything a worker thread needs in order to open this profile.

        ``jump`` is resolved by whoever has the vault open - a spec crosses
        threads, so it holds a plain JumpHost rather than the id of a profile
        it would have to look up.

        This lives here rather than beside a tab because more than one caller
        needs it now: a file-manager tab, and a terminal opened straight from
        the connection list, which asks for the shell form of the same
        profile (see ``transfer/shellaccess.py``).
        """
        return cls(
            kind=profile.kind,
            host=profile.host,
            port=profile.effective_port,
            username=profile.username,
            password=profile.password,
            private_key_path=profile.private_key_path,
            passive=profile.passive,
            use_agent=profile.use_agent,
            use_default_keys=profile.use_default_keys,
            host_key_mode=hostkeys.PROMPT,
            jump=jump,
            proxy_command=profile.proxy_command,
        )

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
                use_agent=self.use_agent,
                use_default_keys=self.use_default_keys,
                host_key_mode=self.host_key_mode,
                jump=self.jump,
                proxy_command=self.proxy_command,
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
    #: This server has never been connected to, so nothing has confirmed it is
    #: the right one. Carries a hostkeys.HostKeyUnknown for the tab to put to
    #: the user; answering yes and reconnecting is the whole of the flow.
    host_key_unknown = pyqtSignal(object)
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
    #: One MCP-bridge operation finished: (request id, succeeded, message).
    #: Deleting and creating a folder report through op_done/op_failed, which
    #: carry no idea of *which* request they belong to - fine for a status
    #: line, useless to a caller blocked on one particular answer while the
    #: user is deleting something else in the same tab.
    bridge_op = pyqtSignal(str, bool, str)

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
        self._ops_fs: RemoteFS | None = None
        self._ops_lock = threading.Lock()
        # Set by close_connection so a side channel still working
        # closes its own session rather than being waited on.
        self._channels_closing = False
        self._queue_totals = [0, 0, 0]  # queued, completed, failed
        self._last_listing = ""        # refreshed after a queue drains
        # When the queue stats were last sent to the panel. Progress arrives
        # thousands of times a second; the header showing a rate and a time
        # remaining wants a few updates a second and no more.
        self._stats_sent = 0.0
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
        except hostkeys.HostKeyUnknown as exc:
            # Not a failure - a question. The tab asks it and, if the answer
            # is yes, records the key and calls this again.
            self.host_key_unknown.emit(exc)
            return
        except TransferError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # backend import errors, unexpected failures
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self._fs = fs
        self._spec = spec
        self._channels_closing = False
        self.connected.emit(banner)
        self.capabilities_ready.emit(fs.capabilities())

    @pyqtSlot()
    def close_connection(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        # Tell the side channels to stop, then close whichever of them are
        # idle. None of these waits: a comparison of a large site or an
        # `rm -rf` of a big tree holds its lock for as long as the job takes,
        # and closing a tab must not sit behind that - the tab teardown gives
        # this three seconds before it abandons the thread, and the abandoned
        # path then calls back in here from the GUI thread, which is the
        # thread that must never block. A busy channel closes its own
        # connection on the way out instead (see _run_ops and _run_tool).
        self._channels_closing = True
        self._close_channel(self._tool_lock, "_tool_fs")
        self._close_channel(self._browse_lock, "_browse_fs")
        self._close_channel(self._ops_lock, "_ops_fs")
        if self._fs is not None:
            self._fs.close()
            self._fs = None
        self.closed.emit()

    def _close_channel(self, lock: threading.Lock, attribute: str) -> bool:
        """Close one side channel if nothing is using it. Never waits."""
        if not lock.acquire(blocking=False):
            return False
        try:
            self._drop_channel(attribute)
        finally:
            lock.release()
        return True

    def _drop_channel(self, attribute: str) -> None:
        """Close and forget one channel's session. The lock is already held."""
        session = getattr(self, attribute, None)
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass
        setattr(self, attribute, None)

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

    @pyqtSlot(object)
    def delete_entries(self, entries: object) -> None:
        """Delete a whole selection, then re-list the directory once.

        Deleting used to arrive one path at a time, and each of those ended by
        re-listing its parent - so a selection of thirty files cost thirty
        deletes *and* thirty full directory listings, in order, on the
        connection the panes browse with. The listings were most of the wait
        and none of the work.

        ``entries`` is a list of ``(path, is_dir)``. The caller has both in the
        listing already, which saves a stat per entry; a symlinked directory
        must arrive as ``is_dir=False`` so the link goes and not its target.
        """
        wanted = [
            (str(path), bool(is_dir)) for path, is_dir in (entries or []) if path
        ]
        if not wanted:
            return
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        showing = fs.parent(wanted[0][0])
        total = len(wanted)

        def job(remote: RemoteFS) -> str:
            removed, failures = delete_paths(
                remote, wanted, on_progress=self._ops_ticker("Deleting")
            )
            for message in failures[:3]:
                self.op_failed.emit(message)
            if not removed:
                return ""
            if total == 1:
                return f"Deleted {remote.basename(wanted[0][0])}"
            kept = f", {len(failures)} kept" if failures else ""
            return f"Deleted {removed} of {total} item(s){kept}"

        self._run_ops("delete", job, refresh=showing)

    @pyqtSlot(str, bool)
    def delete_entry(self, path: str, is_dir: bool) -> None:
        """One path, for callers that still have only one. See delete_entries."""
        self.delete_entries([(path, is_dir)])

    # ----- work submitted by the MCP bridge -------------------------------
    # The same operations as above, answered to one caller rather than to the
    # status line. They deliberately go through _ops_connection like every
    # other operation, so a delete Claude asks for is a delete this tab made:
    # journalled for Undo, and re-listed in the pane when it is done.
    @pyqtSlot(str, object)
    def bridge_delete(self, request_id: str, entries: object) -> None:
        """Delete paths on behalf of the bridge and say what became of them.

        ``is_dir`` may be None, which the file manager never sends because it
        has the listing in front of it - the MCP server does not, and making
        it open a connection purely to stat something it is about to hand over
        would waste the round trip this whole bridge exists to save. Resolved
        below on the connection that is about to do the deleting.
        """
        raw = [(str(path), is_dir) for path, is_dir in (entries or []) if path]
        if not raw:
            self.bridge_op.emit(request_id, False, "nothing to delete")
            return
        fs = self._fs
        if fs is None:
            self.bridge_op.emit(request_id, False, NOT_CONNECTED)
            return
        showing = fs.parent(raw[0][0])
        total = len(raw)

        def job(remote: RemoteFS) -> tuple[bool, str]:
            wanted = []
            for path, is_dir in raw:
                if is_dir is None:
                    info = remote.stat(path)
                    # A symlinked directory has to go as the link it is, not
                    # as the tree it points at - see delete_entries.
                    is_dir = info.is_dir and not info.is_link
                wanted.append((path, bool(is_dir)))
            removed, failures = delete_paths(
                remote, wanted, on_progress=self._ops_ticker("Deleting")
            )
            if failures:
                detail = "; ".join(failures[:3])
                if len(failures) > 3:
                    detail += f"; and {len(failures) - 3} more"
                return (removed > 0, f"Deleted {removed} of {total}: {detail}")
            if total == 1:
                return (True, f"Deleted {wanted[0][0]}")
            return (True, f"Deleted {removed} item(s)")

        self._run_bridge_op(request_id, "delete", job, refresh=showing)

    @pyqtSlot(str, str)
    def bridge_make_dir(self, request_id: str, path: str) -> None:
        """Create a folder and its parents on behalf of the bridge."""
        fs = self._fs
        if fs is None:
            self.bridge_op.emit(request_id, False, NOT_CONNECTED)
            return
        parent = fs.parent(path)

        def job(remote: RemoteFS) -> tuple[bool, str]:
            remote.makedirs(path)
            return (True, f"Created {path} (and any missing parents)")

        self._run_bridge_op(request_id, "mkdir", job, refresh=parent)

    @pyqtSlot(str, str, str, float)
    def bridge_exec(
        self, request_id: str, command: str, cwd: str, timeout: float
    ) -> None:
        """Run one command for the bridge, on this session's own connection.

        The point of it coming here rather than being run in the MCP process:
        this is the connection the user is already logged in on, so nothing
        opens a second session, nothing re-answers a host key, and what Claude
        ran shows up on the tab that ran it.

        A command that exits non-zero is still a command that ran, so it comes
        back as a success carrying the exit status. Only a shell that could
        not be opened at all is a failure.
        """
        from mysql_runner.transfer import remote_exec

        fs = self._fs
        if fs is None:
            self.bridge_op.emit(request_id, False, NOT_CONNECTED)
            return

        def job(remote: RemoteFS) -> tuple[bool, str]:
            result = remote_exec.run(remote, command, cwd=cwd, timeout=timeout)
            return (True, remote_exec.transcript(command, result, cwd=cwd))

        self._run_bridge_op(request_id, "exec", job)

    def _run_bridge_op(self, request_id: str, label: str, job, *, refresh: str = "") -> None:
        """_run_ops, but the outcome goes to one waiting caller.

        Every path out of here emits bridge_op exactly once, including the
        failures: the caller is blocked on it, so a silent return would be a
        caller waiting out its whole timeout for an answer that was decided
        immediately.
        """

        def runner() -> None:
            with self._ops_lock:
                try:
                    ok, message = job(self._ops_connection())
                except Unsupported as exc:
                    self.bridge_op.emit(request_id, False, str(exc))
                    return
                except (TransferError, OSError) as exc:
                    self.bridge_op.emit(request_id, False, str(exc))
                    return
                except Exception as exc:
                    self.bridge_op.emit(
                        request_id, False, str(exc) or exc.__class__.__name__
                    )
                    return
                finally:
                    if self._channels_closing:
                        self._drop_channel("_ops_fs")
            self.bridge_op.emit(request_id, bool(ok), str(message))
            if refresh:
                self._refresh_listing.emit(refresh)

        threading.Thread(
            target=runner, name=f"bridge-{label}", daemon=True
        ).start()

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
        from mysql_runner.transfer.permissions import to_octal

        if not recursive:
            # One round trip; it belongs where the click came from.
            try:
                self._with_session(lambda session: session.chmod(path, mode))
            except TransferError as exc:
                self.op_failed.emit(str(exc))
                return
            self.op_done.emit(f"{fs.basename(path)} is now {to_octal(mode)}")
            self.list_dir(fs.parent(path))
            return

        parent = fs.parent(path)
        name = fs.basename(path)

        def job(remote: RemoteFS) -> str:
            from mysql_runner.transfer.remote_exec import chmod_tree

            chmod_tree(remote, path, mode, scope=scope)
            return f"{name} is now {to_octal(mode)} (recursively)"

        self._run_ops("chmod", job, refresh=parent)

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

        On the operations connection rather than the navigation one. "The pane
        is idle at that moment anyway" was true of the click and not of the
        download: open a 40 MB log to look at the end of it and browsing
        stopped until the whole thing had come down. The tab keeps watching the
        local copy afterwards to upload each save.
        """
        fs = self._fs
        if fs is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        name = fs.basename(remote)

        def job(session: RemoteFS) -> str:
            try:
                os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
                session.download(remote, local)
            except (TransferError, OSError) as exc:
                raise TransferError(f"{name}: {exc}") from exc
            self.edit_ready.emit(local, remote)
            return ""

        self._run_ops("edit", job)

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
        # The panel's header carries a live rate and a time remaining, and
        # nothing else on this path would ever refresh it: stats used to be
        # sent only when the queue changed shape - a file finishing, a pause -
        # so between two large files the figure simply stopped. Throttled,
        # because this runs on every worker several times a second.
        now = time.monotonic()
        if now - self._stats_sent < STATS_INTERVAL:
            return
        self._stats_sent = now
        pool = self._pool
        if pool is not None:
            self.queue_stats.emit(pool.stats())

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
        # One call for every directory in the push, not one round trip each:
        # where the account has a shell they go up in a single `mkdir -p`.
        try:
            fs.makedirs_many(directories)
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
        try:
            fs.makedirs_many(directories)
        except TransferError:
            pass  # a real problem surfaces when the first file lands
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
        # Every directory the whole push needs, collected before any of them
        # is made. Sibling groups share every parent above them, so the set
        # both removes the duplicates and lets the lot go up in one call -
        # on a server with a shell, one `mkdir -p` for a site of two hundred
        # folders instead of two hundred round trips taken in turn.
        wanted: list[str] = []
        seen: set[str] = set()
        for target, sources in groups:
            batch, directories, missed = expand_local(
                fs, sources, target, rules=rules
            )
            jobs.extend(batch)
            skipped.extend(missed)
            # A sync can aim files at a directory that does not exist yet -
            # a commit that adds a folder. The directory on show obviously
            # exists, so it is not paid for.
            for path in [target, *directories]:
                if not path or path in seen or path == self._last_listing:
                    continue
                seen.add(path)
                wanted.append(path)
        try:
            fs.makedirs_many(wanted)
        except TransferError:
            pass  # a real problem surfaces when the first file lands
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
            # The whole set, not just the worker count: a speed limit reached
            # for in the middle of a large upload is somebody asking for their
            # link back now, not at the end of the queue.
            self._pool.update_options(self._options)

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
                if self._channels_closing:
                    self._drop_channel("_browse_fs")
                    return
            names = sorted(
                (entry.name for entry in entries if entry.is_dir),
                key=str.lower,
            )
            self.folders_listed.emit(target, names)

        threading.Thread(target=runner, name="browse-folders", daemon=True).start()

    # ----- operations (their own connection, one at a time) ---------------
    # The write-side twin of the tool channel. Kept apart from it because the
    # two must not block each other - a comparison of a large site can hold the
    # tool connection for a minute, and "delete this folder" should not wait
    # for it - and apart from the navigation connection because these jobs walk
    # trees, which is exactly what browsing must never queue behind.
    def _ops_connection(self) -> RemoteFS:
        """The mutating jobs' own session. Call with ``_ops_lock`` held."""
        if self._ops_fs is not None:
            if self._ops_fs.alive():
                return self._ops_fs
            try:
                self._ops_fs.close()
            except Exception:
                pass
            self._ops_fs = None
        spec = self._spec
        if spec is None or self._channels_closing:
            raise TransferError(NOT_CONNECTED)
        self._ops_fs = spec.connected()
        return self._ops_fs

    def _run_ops(self, label: str, job, *, refresh: str = "") -> None:
        """Run ``job(fs)`` off the worker thread and report what it returned.

        Serialised rather than refused: two deletes in a row are a perfectly
        ordinary thing to ask for, and the second one waiting a moment is a
        better answer than "still busy with the previous job".

        ``refresh`` names a directory to re-list afterwards - once, at the end,
        which is the entire point. The listing is emitted rather than called,
        because it has to happen on the thread that owns the navigation
        connection and this is not that thread.
        """

        def runner() -> None:
            message = ""
            with self._ops_lock:
                try:
                    message = job(self._ops_connection())
                except Unsupported as exc:
                    self.op_failed.emit(str(exc))
                    return
                except (TransferError, OSError) as exc:
                    self.op_failed.emit(str(exc))
                    return
                except Exception as exc:
                    self.op_failed.emit(str(exc) or exc.__class__.__name__)
                    return
                finally:
                    # close_connection will not wait for a job this long, so
                    # the job is what closes the session it was using.
                    if self._channels_closing:
                        self._drop_channel("_ops_fs")
            if message:
                self.op_done.emit(str(message))
            if refresh:
                self._refresh_listing.emit(refresh)

        threading.Thread(target=runner, name=f"ops-{label}", daemon=True).start()

    def _ops_ticker(self, verb: str):
        """A progress callback for an ops job, throttled to the status line.

        A thousand-file delete would otherwise send a thousand messages to a
        line that can show one, and every one of them crosses a thread.
        """
        state = {"last": 0.0}

        def report(done: int, total: int, name: str) -> None:
            if total <= 1:
                return
            now = time.monotonic()
            if now - state["last"] < OPS_PROGRESS_INTERVAL:
                return
            state["last"] = now
            self.op_done.emit(f"{verb} {done} of {total}: {name}")

        return report

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
                if self._channels_closing:
                    # As in _run_ops: nobody is waiting on this channel's lock
                    # any more, so the job closes its own session.
                    with self._tool_lock:
                        self._drop_channel("_tool_fs")

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

        :meth:`delete_entries` re-lists the parent of what it deleted, which is
        right for a deliberate delete and wrong for a background sync - it would
        yank the remote pane off to wherever the deleted file happened to live.
        This deletes the lot, then refreshes only the directory already on show.
        """
        if not isinstance(paths, list) or not paths:
            return
        if self._spec is None:
            self.op_failed.emit(NOT_CONNECTED)
            return
        wanted = [str(path) for path in paths if path]
        if not wanted:
            return

        def job(remote: RemoteFS) -> str:
            removed, failures = delete_paths(
                remote, wanted, on_progress=self._ops_ticker("Removing")
            )
            for message in failures[:3]:
                self.op_failed.emit(message)
            if removed and self._last_listing:
                # Only the directory already on show, and only when something
                # actually went: a background sync must not move the pane.
                self._refresh_listing.emit(self._last_listing)
            return f"Removed {removed} file(s) on the server" if removed else ""

        self._run_ops("sync-delete", job)

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
