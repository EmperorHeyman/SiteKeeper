"""FTP/FTPS/SFTP sessions for the dual-pane view, without Qt.

The Qt build runs a QObject worker on a QThread and reports progress through
signals; here the same shared core does the work and progress is pushed to the
webview over the WebSocket hub. Nothing about the transfer logic is duplicated:
the queue is ``transfer.pool.TransferPool``, the expansion, hashing, ignore
rules and shadow backups are the same modules the desktop app uses.

Each session owns three things, for the same reason the Qt tab does:

* a **navigation** connection for listings and small operations,
* the **pool**, which opens its own connections for the queue, and
* a **tools** connection for the slow read-only jobs.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from app.core.ws import hub
from mysql_runner.storage.models import ConnectionKind, ServerProfile
from mysql_runner.storage.settings import Settings
from mysql_runner.transfer.base import Capability, RemoteFS, TransferError
from mysql_runner.transfer.history import HistoryStore
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.removal import delete_tree
from mysql_runner.transfer.pool import (
    Overwrite,
    PoolEvents,
    PoolOptions,
    TransferItem,
    TransferPool,
    expand_local,
    expand_remote,
)


class SessionNotFound(KeyError):
    """Raised when a session id does not name an open connection."""


def _build_backend(profile: ServerProfile) -> RemoteFS:
    """Instantiate the backend for a profile's protocol."""
    if profile.kind == ConnectionKind.SFTP:
        from mysql_runner.transfer.sftp_client import SFTPFileSystem

        return SFTPFileSystem(
            profile.host,
            profile.effective_port,
            profile.username,
            profile.password,
            private_key_path=profile.private_key_path,
        )
    from mysql_runner.transfer.ftp_client import FTPFileSystem

    return FTPFileSystem(
        profile.host,
        profile.effective_port,
        profile.username,
        profile.password,
        use_tls=profile.kind == ConnectionKind.FTPS,
        passive=profile.passive,
    )


def _connected(profile: ServerProfile) -> RemoteFS:
    fs = _build_backend(profile)
    fs.connect()
    return fs


def _options_from_settings(settings: Settings) -> PoolOptions:
    return PoolOptions(
        workers=settings.transfer_workers,
        atomic=settings.atomic_uploads,
        keep_backups=settings.shadow_backups,
        overwrite=Overwrite.ALWAYS,
        verify=settings.verify_uploads,
        rate_limit=max(0, settings.transfer_rate_kb) * 1024,
    ).sane()


def entry_dict(entry) -> dict:
    return {
        "name": entry.name,
        "is_dir": entry.is_dir,
        "size": entry.size,
        "modified": entry.modified,
        "is_link": entry.is_link,
        "mode": entry.mode,
        "link_target": entry.link_target,
    }


def item_dict(item: TransferItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "upload": item.upload,
        "local": item.local,
        "remote": item.remote,
        "size": item.size,
        "transferred": item.transferred,
        "state": item.state.value,
        "error": item.error,
        "note": item.note,
        "priority": item.priority,
        "fraction": round(item.fraction, 4),
        "rate": int(item.rate),
    }


class TransferSession:
    """A connected remote filesystem, its transfer pool and its tool channel."""

    def __init__(self, profile: ServerProfile, settings: Settings | None = None) -> None:
        self.id = uuid.uuid4().hex
        self.profile_id = profile.id
        self.label = profile.label
        self.kind = profile.kind.value
        self.target = profile.describe_target()
        self.opened_at = time.time()
        self.banner = ""
        self._profile = profile
        self._settings = settings or Settings.load()
        self._options = _options_from_settings(self._settings)
        self._fs: RemoteFS | None = None
        self._lock = threading.Lock()
        self._tool_fs: RemoteFS | None = None
        self._tool_lock = threading.Lock()
        self._pool: TransferPool | None = None
        self._history = HistoryStore() if self._settings.shadow_backups else None
        self._counts = [0, 0]  # completed, failed for the current queue

    # ----- lifecycle ------------------------------------------------------
    def open(self, profile: ServerProfile) -> dict:
        fs = _build_backend(profile)
        self.banner = fs.connect()
        self._fs = fs
        return self.info()

    def close(self) -> None:
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
        with self._lock:
            if self._fs is not None:
                self._fs.close()
                self._fs = None

    def info(self) -> dict:
        fs = self._fs
        capabilities = sorted(c.value for c in (fs.capabilities() if fs else frozenset()))
        stats = self._pool.stats() if self._pool is not None else {}
        return {
            "session_id": self.id,
            "profile_id": self.profile_id,
            "label": self.label,
            "kind": self.kind,
            "target": self.target,
            "banner": self.banner,
            "opened_at": self.opened_at,
            "busy": bool(stats.get("counts", {}).get("running")),
            "capabilities": capabilities,
            "can_exec": Capability.EXEC.value in capabilities,
            "options": {
                "workers": self._options.workers,
                "atomic": self._options.atomic,
                "keep_backups": self._options.keep_backups,
                "verify": self._options.verify,
                "use_ignore_rules": self._settings.use_ignore_rules,
            },
        }

    def _require(self) -> RemoteFS:
        fs = self._fs
        if fs is None:
            raise TransferError("Not connected.")
        return fs

    def _tools(self) -> RemoteFS:
        """The read-only connection the slow jobs use."""
        with self._tool_lock:
            if self._tool_fs is None:
                self._tool_fs = _connected(self._profile)
            return self._tool_fs

    def _rules(self, enabled: bool | None = None) -> IgnoreRules:
        use = self._settings.use_ignore_rules if enabled is None else enabled
        if not use:
            return IgnoreRules.empty()
        return IgnoreRules.defaults() if self._settings.ignore_defaults else IgnoreRules.empty()

    def _local_rules(self, directory: str, enabled: bool | None = None) -> IgnoreRules:
        """Rules including any .deployignore in the local directory."""
        use = self._settings.use_ignore_rules if enabled is None else enabled
        if not use or not directory or not os.path.isdir(directory):
            return self._rules(enabled)
        return IgnoreRules.from_local_dir(
            directory, with_defaults=self._settings.ignore_defaults
        )

    # ----- navigation -----------------------------------------------------
    def home(self) -> str:
        with self._lock:
            return self._require().home()

    def listdir(self, path: str) -> dict:
        with self._lock:
            fs = self._require()
            entries = fs.listdir(path or "/")
        return {
            "path": path or "/",
            "at_root": (path or "/") in ("/", ""),
            "entries": [entry_dict(entry) for entry in entries],
        }

    def mkdir(self, path: str) -> None:
        with self._lock:
            self._require().mkdir(path)

    def delete(self, path: str, is_dir: bool) -> None:
        with self._lock:
            fs = self._require()
            if is_dir:
                delete_tree(fs, path)
            else:
                fs.remove(path)

    def rename(self, source: str, target: str) -> None:
        with self._lock:
            self._require().rename(source, target)

    def chmod(self, path: str, mode: int, *, recursive: bool = False, scope: str = "all") -> None:
        with self._lock:
            fs = self._require()
            if not recursive:
                fs.chmod(path, mode)
                return
        from mysql_runner.transfer.remote_exec import chmod_tree

        chmod_tree(self._tools(), path, mode, scope=scope)

    def symlink(self, target: str, link_path: str) -> None:
        with self._lock:
            fs = self._require()
            if fs.exists(link_path):
                fs.remove(link_path)
            fs.symlink(target, link_path)

    def link_target(self, path: str) -> str:
        with self._lock:
            return self._require().readlink(path)

    # ----- the queue ------------------------------------------------------
    def _ensure_pool(self) -> TransferPool:
        if self._pool is None:
            events = PoolEvents(
                on_item=self._on_item,
                on_progress=self._on_progress,
                on_message=lambda text: self._emit("transfer.message", {"message": text}),
                on_idle=self._on_idle,
            )
            self._pool = TransferPool(
                lambda: _connected(self._profile),
                options=self._options,
                events=events,
                history=self._history,
                profile_id=self.profile_id,
                profile_label=self.label,
            )
        return self._pool

    def _emit(self, event: str, payload: dict) -> None:
        hub.broadcast_threadsafe(event, {"session_id": self.id, **payload})

    def _on_item(self, item: TransferItem) -> None:
        from mysql_runner.transfer.pool import JobState

        self._emit("transfer.item", item_dict(item))
        if item.state == JobState.DONE:
            self._counts[0] += 1
            self._emit("transfer.file_done", {"name": item.name})
        elif item.state == JobState.FAILED:
            self._counts[1] += 1
            self._emit("transfer.error", {"name": item.name, "message": item.error})

    def _on_progress(self, item: TransferItem) -> None:
        self._emit(
            "transfer.progress",
            {
                "name": item.name,
                "transferred": item.transferred,
                "total": item.size,
                "id": item.id,
            },
        )

    def _on_idle(self, stats: dict) -> None:
        counts = stats.get("counts", {})
        self._emit("transfer.stats", stats)
        self._emit(
            "transfer.finished",
            {
                "completed": self._counts[0],
                "failed": self._counts[1],
                "cancelled": bool(counts.get("cancelled")),
            },
        )
        self._counts = [0, 0]

    def enqueue(
        self,
        upload: bool,
        items: list[tuple[str, bool]],
        target: str,
        *,
        use_ignore: bool | None = None,
    ) -> dict:
        pool = self._ensure_pool()
        with self._lock:
            fs = self._require()
            if upload:
                rules = self._local_rules(_common_parent(items), use_ignore)
                jobs, dirs, skipped = expand_local(fs, items, target, rules=rules)
                for path in dirs:
                    try:
                        fs.mkdir(path)
                    except TransferError:
                        pass  # nearly always "already exists"
            else:
                rules = self._rules(use_ignore)
                jobs, dirs, skipped = expand_remote(fs, items, target, rules=rules)
                for path in dirs:
                    os.makedirs(path, exist_ok=True)
        self._counts = [0, 0]
        self._emit("transfer.started", {"total": len(jobs), "skipped": len(skipped)})
        if jobs:
            pool.submit(jobs)
        else:
            self._on_idle(pool.stats())
        return {
            "queued": len(jobs),
            "skipped": skipped[:50],
            "skipped_count": len(skipped),
        }

    def queue(self) -> dict:
        if self._pool is None:
            return {"items": [], "stats": {}}
        return {
            "items": [item_dict(item) for item in self._pool.items()],
            "stats": self._pool.stats(),
        }

    def pause(self) -> None:
        self._ensure_pool().pause()

    def resume(self) -> None:
        self._ensure_pool().resume()

    def cancel(self) -> None:
        if self._pool is not None:
            self._pool.cancel_all()

    def cancel_item(self, item_id: str) -> bool:
        return bool(self._pool is not None and self._pool.cancel(item_id))

    def prioritize(self, item_id: str) -> bool:
        return bool(self._pool is not None and self._pool.prioritize(item_id))

    def reorder(self, item_ids: list[str]) -> None:
        if self._pool is not None:
            self._pool.reorder(item_ids)

    def clear_finished(self) -> int:
        return self._pool.clear_finished() if self._pool is not None else 0

    def set_options(self, **changes) -> dict:
        """Change the pool's behaviour for this session."""
        if "workers" in changes and changes["workers"]:
            self._options.workers = int(changes["workers"])
            if self._pool is not None:
                self._pool.set_workers(self._options.workers)
        for name in ("atomic", "keep_backups", "verify"):
            if changes.get(name) is not None:
                setattr(self._options, name, bool(changes[name]))
        if changes.get("use_ignore_rules") is not None:
            self._settings.use_ignore_rules = bool(changes["use_ignore_rules"])
        self._options = self._options.sane()
        return self.info()["options"]

    # ----- tools ----------------------------------------------------------
    def compare(
        self,
        local_dir: str,
        remote_dir: str,
        *,
        with_hashes: bool = True,
        use_ignore: bool | None = None,
    ) -> dict:
        from mysql_runner.transfer.hashing import compare, snapshot_local, snapshot_remote

        rules = self._local_rules(local_dir, use_ignore)
        local = snapshot_local(local_dir, rules=rules, with_hashes=with_hashes)
        remote = snapshot_remote(
            self._tools(), remote_dir, rules=rules, with_hashes=with_hashes
        )
        report = compare(local, remote)
        return {
            "local_dir": local_dir,
            "remote_dir": remote_dir,
            "compared_by": report.compared_by,
            "counts": report.counts(),
            "summary": report.summary(),
            "statuses": {rel: status.value for rel, status in report.statuses.items()},
            "truncated": local.truncated or remote.truncated,
            "errors": (local.errors + remote.errors)[:20],
        }

    def folder_stats(self, parent: str, names: list[str]) -> dict:
        from mysql_runner.transfer.treestat import remote_folder_stats

        fs = self._tools()
        out: dict[str, dict] = {}
        for name in names:
            try:
                stats = remote_folder_stats(fs, fs.join(parent, name))
            except TransferError:
                continue
            out[name] = {
                "size": stats.size,
                "files": stats.files,
                "dirs": stats.dirs,
                "newest": stats.newest,
                "truncated": stats.truncated,
            }
        return {"parent": parent, "stats": out}

    def digest(self, path: str) -> dict:
        from mysql_runner.transfer.hashing import hash_remote_file

        return {"path": path, "digest": hash_remote_file(self._tools(), path)}

    def grep(
        self,
        root: str,
        pattern: str,
        *,
        fixed: bool = True,
        ignore_case: bool = False,
        include: str = "",
    ) -> dict:
        from mysql_runner.transfer.remote_exec import grep

        result = grep(
            self._tools(),
            root,
            pattern,
            fixed=fixed,
            ignore_case=ignore_case,
            include=include,
        )
        return {
            "tool": result.tool,
            "truncated": result.truncated,
            "error": result.error,
            "hits": [
                {"path": hit.path, "line": hit.line, "text": hit.text}
                for hit in result.hits
            ],
        }

    def disk_usage(self, path: str) -> dict:
        from mysql_runner.transfer.remote_exec import disk_usage

        usage = disk_usage(self._tools(), path)
        return {
            "root": usage.root,
            "total": usage.total,
            "entries": [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "size": entry.size,
                    "share": round(usage.share(entry), 4),
                }
                for entry in usage.entries
            ],
        }

    def run_command(self, command: str, cwd: str = "") -> dict:
        from mysql_runner.transfer.remote_exec import run

        result = run(self._tools(), command, cwd=cwd, timeout=120)
        return {
            "command": result.command,
            "exit_status": result.exit_status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "ok": result.ok,
        }

    def logs(self, directory: str) -> list[str]:
        from mysql_runner.transfer.remote_exec import list_logs

        return list_logs(self._tools(), directory)

    def tail(self, path: str, *, lines: int = 200) -> str:
        """One snapshot of the end of a log file (the web view polls this)."""
        from mysql_runner.transfer.remote_exec import quote, run

        result = run(
            self._tools(), f"tail -n {int(lines)} -- {quote(path)}", timeout=60
        )
        return result.stdout

    def archive(self, directory: str, names: list[str], archive: str, kind: str) -> dict:
        from mysql_runner.transfer.remote_exec import make_archive

        make_archive(self._tools(), directory, names, archive, kind=kind)
        return {"archive": archive}

    def extract(self, archive: str, destination: str) -> dict:
        from mysql_runner.transfer.remote_exec import extract_archive

        extract_archive(self._tools(), archive, destination)
        return {"destination": destination}

    # ----- history --------------------------------------------------------
    def history(self, limit: int = 100) -> list[dict]:
        store = self._history or HistoryStore()
        return [
            {
                "id": entry.id,
                "action": entry.action.value,
                "target": entry.target,
                "name": entry.name,
                "size": entry.size,
                "when": entry.when,
                "note": entry.note,
                "undone": entry.undone,
                "can_undo": entry.can_undo,
                "describe": entry.describe(),
            }
            for entry in store.entries(profile_id=self.profile_id, limit=limit)
        ]

    def undo(self, entry_id: str) -> str:
        store = self._history or HistoryStore()
        with self._lock:
            fs = self._require()
            return store.undo(entry_id, fs)


def _common_parent(items: list[tuple[str, bool]]) -> str:
    """The local directory a selection came from, for its ignore file."""
    for path, is_dir in items:
        return path if is_dir else os.path.dirname(path)
    return ""


class TransferSessionManager:
    """Owns every open transfer session in the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, TransferSession] = {}

    def open(self, profile: ServerProfile) -> dict:
        session = TransferSession(profile)
        info = session.open(profile)
        with self._lock:
            self._sessions[session.id] = session
        return info

    def get(self, session_id: str) -> TransferSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionNotFound(session_id)
        session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list(self) -> list[dict]:
        with self._lock:
            return [s.info() for s in self._sessions.values()]


manager = TransferSessionManager()
