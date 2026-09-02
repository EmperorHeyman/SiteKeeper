"""The tools Claude can call, and the guarded access they go through.

Everything here is deliberately headless and Qt-free: the vault unlocks the
way the app does (Windows-sealed key, or the DEK cached in the OS keyring by
a previous unlock - ``SITEKEEPER_MASTER_PASSWORD`` is the fallback for
machines that cache nothing), profiles come from the same encrypted store,
and connections use the same FTP/FTPS/SFTP backends as the file manager.

The safety model is opt-in, and what it reads is the app's own grants file
rather than this process's command line - see ``mcp/policy.py`` for why:

* reading (listings, file contents, downloads, SELECTs) is always allowed,
* uploads and mkdir need "Upload files and create folders",
* deleting anything on a server needs "Delete files and folders",
* SQL that changes data needs "Run SQL that changes data",
* and none of the above touch a connection marked PRODUCTION until that
  connection is granted production by name.

Each of those is a tick in Sitekeeper's "Connect Claude" window, re-read on
every tool call, so switching one on lands on the next thing Claude tries
rather than the next time the server starts.

Tool output is plain text sized for a model to read, never credentials.
"""

from __future__ import annotations

import io
import os

from mysql_runner.mcp.policy import LivePolicy, McpPolicy
from mysql_runner.storage.models import ConnectionKind, Environment, ServerProfile
from mysql_runner.transfer import bridge
from mysql_runner.transfer.base import (
    Capability,
    RemoteFS,
    TransferError,
    local_relative,
)
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.removal import UNCOUNTED, delete_tree

#: In-memory reads are for configs and logs, not for site archives.
MAX_READ_BYTES = 2 * 1024 * 1024
DEFAULT_READ_BYTES = 256 * 1024
#: One tool call pushes at most this many files; more deserves the app.
MAX_FOLDER_FILES = 500
#: Rows shown per statement. The driver caps its own fetch far higher.
MAX_RESULT_ROWS = 200

#: First words of statements that read without changing anything.
READ_ONLY_SQL = frozenset(
    ("select", "show", "describe", "desc", "explain", "use", "help")
)


class ToolError(Exception):
    """A refusal or failure whose text goes straight back to the model."""


#: Where in the app the grants are ticked, so a refusal can say where to go.
WINDOW = 'the "Connect Claude" window in Sitekeeper (Tools -> Connect Claude)'

#: Policy attribute -> the label of the checkbox that sets it. A refusal that
#: names the box is one the reader can act on; "--allow-write" was a string
#: they had to already know the meaning of.
GRANT_LABELS = {
    "allow_write": "Upload files and create folders",
    "allow_delete": "Delete files and folders",
    "allow_sql_write": "Run SQL that changes data",
}


class AppAccess:
    """The vault, the profiles, and one live connection per server."""

    def __init__(self) -> None:
        self._policy = LivePolicy()
        self._store = None
        self._remotes: dict[str, RemoteFS] = {}

    @property
    def policy(self) -> McpPolicy:
        """The grants as they stand right now, not as they stood at startup."""
        return self._policy.current()

    # ----- the vault --------------------------------------------------------
    def _unlock(self):
        from mysql_runner.crypto import vault as vaultmod
        from mysql_runner.storage.store import ServerStore, StoreError, opens_store

        if not vaultmod.is_initialized():
            raise ToolError(
                "No Sitekeeper vault exists on this machine yet. Open the app "
                "once and add your servers first."
            )
        if vaultmod.protection_mode() == vaultmod.PROTECTION_WINDOWS:
            vault = vaultmod.unlock_keyless()
        else:
            vault = vaultmod.unlock_with_keyring()
            if vault is not None and not opens_store(vault):
                vault = None  # a stale cache from another install
            if vault is None:
                password = os.getenv("SITEKEEPER_MASTER_PASSWORD", "")
                if not password:
                    raise ToolError(
                        "The vault is locked. Unlock Sitekeeper once so the "
                        "key is cached, or set SITEKEEPER_MASTER_PASSWORD in "
                        "this server's environment."
                    )
                vault = vaultmod.unlock_with_password(password)
        try:
            return ServerStore(vault)
        except StoreError as exc:
            raise ToolError(str(exc)) from exc

    def store(self):
        if self._store is None:
            self._store = self._unlock()
        return self._store

    # ----- profiles ----------------------------------------------------------
    def profiles(self) -> list[ServerProfile]:
        policy = self.policy
        return [p for p in self.store().all() if policy.sees(p)]

    def profile(self, ref: str) -> ServerProfile:
        """Find a profile by label (case-insensitive) or id prefix."""
        ref = (ref or "").strip()
        if not ref:
            raise ToolError("Say which profile: " + self._catalogue())
        needle = ref.casefold()
        candidates = self.profiles()
        for profile in candidates:
            if profile.label.casefold() == needle:
                return profile
        for profile in candidates:
            if profile.id.startswith(ref):
                return profile
        raise ToolError(f"No profile called {ref!r}. Known: " + self._catalogue())

    def _catalogue(self) -> str:
        labels = [p.label for p in self.profiles()]
        return ", ".join(labels) if labels else "(none stored)"

    # ----- connections --------------------------------------------------------
    def remote(self, profile: ServerProfile) -> RemoteFS:
        if not profile.kind.is_transfer:
            raise ToolError(
                f"{profile.label} is a {profile.kind.value} profile; only "
                "FTP, FTPS and SFTP profiles have a remote filesystem."
            )
        cached = self._remotes.get(profile.id)
        if cached is not None:
            if cached.alive():
                return cached
            try:
                cached.close()
            except Exception:
                pass
            del self._remotes[profile.id]
        fs = self._build(profile)
        fs.connect()
        self._remotes[profile.id] = fs
        return fs

    @staticmethod
    def _build(profile: ServerProfile) -> RemoteFS:
        # The same wiring as the app's ConnectionSpec, which lives next to Qt
        # and therefore cannot be imported here.
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

    def close(self) -> None:
        for fs in self._remotes.values():
            try:
                fs.close()
            except Exception:
                pass
        self._remotes.clear()

    # ----- permission gates ----------------------------------------------------
    def guard(self, profile: ServerProfile, action: str, grant: str) -> None:
        """Refuse unless the app grants this, on this connection, right now."""
        policy = self.policy
        if not getattr(policy, grant):
            raise ToolError(
                f"{action} is not switched on for Claude. Tick "
                f'"{GRANT_LABELS[grant]}" in {WINDOW}, then try again - it '
                "takes effect at once, with nothing to restart."
            )
        if profile.environment == Environment.PROD and not policy.allows_production(
            profile
        ):
            raise ToolError(
                f"{profile.label} is marked PRODUCTION, and production is "
                "granted per connection. Tick it beside that connection in "
                f"{WINDOW}, then try again - it takes effect at once, with "
                "nothing to restart."
            )


# ----- helpers ---------------------------------------------------------------
def _human_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _human_time(epoch: float | None) -> str:
    if not epoch:
        return "                "
    from datetime import datetime

    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "                "


def _preserve_mtime(fs: RemoteFS, local: str, remote: str) -> None:
    if not fs.supports(Capability.SET_MTIME):
        return
    try:
        fs.set_mtime(remote, os.path.getmtime(local))
    except (TransferError, OSError):
        pass  # cosmetic; never fail an upload over it


def _hand_over(
    profile, pairs: list[tuple[str, str]], note: str, *, download: bool = False
) -> str | None:
    """Give these uploads to a running Sitekeeper, or None if there is none.

    This is what puts Claude's transfers in the app's queue - as real rows
    that can be cancelled and reordered, backed by the shadow-backup journal
    so "undo replace" can put back whatever they overwrote, under the same
    rate limit and written atomically. Doing it here in this process, which is
    what used to happen, got none of that and showed nothing anywhere.

    A missing app, or one with no tab open on this connection, is not a
    failure: None sends the caller back to its own uploading, which is what it
    did before there was a bridge at all.
    """
    try:
        reply = bridge.call(
            {
                "op": "download" if download else "upload",
                "profile_id": profile.id,
                "profile": profile.label,
                "note": note,
                "items": [{"local": local, "remote": remote} for local, remote in pairs],
            }
        )
    except bridge.BridgeUnavailable:
        return None
    except bridge.BridgeError as exc:
        raise ToolError(str(exc)) from exc

    sent = int(reply.get("sent", 0))
    total = int(reply.get("total", len(pairs)))
    failed = [str(line) for line in reply.get("failed") or []]
    verb = "Downloaded" if download else "Uploaded"
    where = "from" if download else "to"
    lines = [
        f"{verb} {sent}/{total} file(s) {where} {profile.label} through the "
        "Sitekeeper queue (cancellable there, and undoable with Undo replace)."
    ]
    lines += [f"FAILED {line}" for line in failed[:5]]
    if len(failed) > 5:
        lines.append(f"... and {len(failed) - 5} more failures.")
    if reply.get("abandoned"):
        lines.append(f"Stopped early: {reply['abandoned']}.")
    return "\n".join(lines)


def _hand_op(profile, op: str, **fields) -> str | None:
    """Give one non-transfer operation to a running Sitekeeper.

    Deleting matters most here. It is the only destructive thing Claude can
    do that has an undo, and the undo is the app's shadow-backup journal - a
    delete performed in this process could not be put back from the app,
    which is exactly the wrong way round for the one operation nobody wants
    to get wrong.

    None means there is nobody to hand it to, and the caller does it itself.
    """
    try:
        reply = bridge.call({"op": op, "profile_id": profile.id, **fields})
    except bridge.BridgeUnavailable:
        return None
    except bridge.BridgeError as exc:
        # The app tried and the server said no. Doing it again directly would
        # fail the same way and skip the journal while it did.
        raise ToolError(str(exc)) from exc
    detail = str(reply.get("detail") or "Done.")
    if op == "query":
        # The output is the answer; the note says where it ran.
        return f"{detail}\n\n(run in Sitekeeper's SQL console for {profile.label})"
    return f"{detail} (through Sitekeeper, so it is in this tab's history)"


def _plan_folder(local_dir: str, remote_dir: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Map a local tree onto the server: ([(local, remote)...], remote dirs)."""
    rules = IgnoreRules.from_local_dir(local_dir, with_defaults=True)
    uploads: list[tuple[str, str]] = []
    directories: list[str] = []
    for current, dirnames, filenames in os.walk(local_dir):
        rel = local_relative(local_dir, current)
        rel = "" if rel == "." else rel
        keep = []
        for name in sorted(dirnames):
            child = f"{rel}/{name}" if rel else name
            if rules.is_ignored(child, is_dir=True) or os.path.islink(
                os.path.join(current, name)
            ):
                continue
            keep.append(name)
            directories.append(RemoteFS.join(remote_dir, child))
        dirnames[:] = keep
        for name in sorted(filenames):
            child = f"{rel}/{name}" if rel else name
            if rules.is_ignored(child):
                continue
            uploads.append(
                (os.path.join(current, name), RemoteFS.join(remote_dir, child))
            )
    return uploads, directories


# ----- the tools themselves ----------------------------------------------------
def list_profiles(access: AppAccess, _args: dict) -> str:
    policy = access.policy
    profiles = access.profiles()
    if not profiles:
        return (
            "No connections are in scope. Either none are stored yet, or "
            f"{WINDOW} is limiting Claude to connections that no longer exist."
        )
    lines = [f"{len(profiles)} profile(s):"]
    for p in profiles:
        env = f" [{p.environment.value.upper()}]" if p.environment != Environment.NONE else ""
        # A PROD connection that has not been granted production is worth
        # saying so about here rather than only when a write is refused.
        if p.environment == Environment.PROD and not policy.allows_production(p):
            env += " (read-only: production not granted)"
        lines.append(f"- {p.label} — {p.kind.value} {p.describe_target()}{env}")
    lines.append("")
    lines.append(f"Claude is {policy.describe()}. Change it in {WINDOW}.")
    return "\n".join(lines)


def list_remote_dir(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    path = str(args.get("path", "")).strip() or profile.remote_dir.strip() or fs.home()
    entries = fs.listdir(path)
    lines = [f"{path} — {len(entries)} entr(y/ies):"]
    for entry in entries:
        kind = "dir " if entry.is_dir else "file"
        size = "" if entry.is_dir else _human_size(entry.size).rjust(9)
        name = entry.name + (f" -> {entry.link_target}" if entry.is_link and entry.link_target else "")
        lines.append(f"{kind}  {_human_time(entry.modified)}  {size:>9}  {name}")
    return "\n".join(lines)


def read_remote_file(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    path = str(args.get("path", "")).strip()
    if not path:
        raise ToolError("Say which remote file to read (path).")
    cap = min(int(args.get("max_bytes", DEFAULT_READ_BYTES) or DEFAULT_READ_BYTES), MAX_READ_BYTES)
    stat = fs.stat(path)
    if stat.is_dir:
        raise ToolError(f"{path} is a directory; use list_remote_dir.")
    if stat.size > cap:
        raise ToolError(
            f"{path} is {_human_size(stat.size)}, over this call's "
            f"{_human_size(cap)} limit. Raise max_bytes (up to "
            f"{_human_size(MAX_READ_BYTES)}) or use download_file."
        )
    buffer = io.BytesIO()
    fs.stream_download(path, buffer.write)
    data = buffer.getvalue()
    if b"\x00" in data:
        return f"{path} is binary ({_human_size(len(data))}); use download_file to fetch it."
    return data.decode("utf-8", errors="replace")


def download_file(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    remote = str(args.get("remote_path", "")).strip()
    local = str(args.get("local_path", "")).strip()
    if not remote or not local:
        raise ToolError("Both remote_path and local_path are required.")
    handed = _hand_over(
        profile,
        [(local, remote)],
        RemoteFS.basename(remote),
        download=True,
    )
    if handed is not None:
        return handed
    fs = access.remote(profile)
    os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
    fs.download(remote, local)
    return f"Downloaded {remote} -> {local} ({_human_size(os.path.getsize(local))})."


def upload_file(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Uploading", "allow_write")
    local = str(args.get("local_path", "")).strip()
    remote = str(args.get("remote_path", "")).strip()
    if not local or not remote:
        raise ToolError("Both local_path and remote_path are required.")
    if not os.path.isfile(local):
        raise ToolError(f"{local} is not a file on this machine.")
    if remote.endswith("/"):
        remote = RemoteFS.join(remote, os.path.basename(local))
    # Before dialling anything: if the app is up and holding this connection,
    # the upload is its job and this process never needs to connect at all.
    handed = _hand_over(profile, [(local, remote)], os.path.basename(local))
    if handed is not None:
        return handed
    fs = access.remote(profile)
    parent = RemoteFS.parent(remote)
    if parent not in ("", "/"):
        fs.makedirs(parent)
    fs.upload(local, remote)
    _preserve_mtime(fs, local, remote)
    return f"Uploaded {local} -> {remote} ({_human_size(os.path.getsize(local))})."


def upload_folder(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Uploading", "allow_write")
    local_dir = str(args.get("local_dir", "")).strip()
    remote_dir = str(args.get("remote_dir", "")).strip()
    if not local_dir or not remote_dir:
        raise ToolError("Both local_dir and remote_dir are required.")
    if not os.path.isdir(local_dir):
        raise ToolError(f"{local_dir} is not a directory on this machine.")
    uploads, directories = _plan_folder(local_dir, remote_dir)
    if len(uploads) > MAX_FOLDER_FILES:
        raise ToolError(
            f"That folder holds {len(uploads)} files after ignore rules; one "
            f"call carries at most {MAX_FOLDER_FILES}. Push a subfolder, or "
            "use the app for whole-site deploys."
        )
    if not uploads:
        return f"Nothing to upload: {local_dir} is empty after the ignore rules."
    handed = _hand_over(
        profile, uploads, f"{os.path.basename(local_dir) or local_dir} -> {remote_dir}"
    )
    if handed is not None:
        return handed
    fs = access.remote(profile)
    # Every directory in one call: on a server with a shell that is a single
    # `mkdir -p`, rather than a round trip per folder before a byte moves.
    fs.makedirs_many([remote_dir, *directories])
    sent = 0
    failures: list[str] = []
    for local, remote in uploads:
        try:
            fs.upload(local, remote)
            _preserve_mtime(fs, local, remote)
            sent += 1
        except TransferError as exc:
            failures.append(f"{remote}: {exc}")
            if len(failures) >= 5:
                failures.append("… stopping after 5 failures.")
                break
    lines = [f"Uploaded {sent}/{len(uploads)} file(s) from {local_dir} to {remote_dir}."]
    lines += [f"FAILED {line}" for line in failures]
    lines += [f"  {local_relative(local_dir, local)}" for local, _ in uploads[:50]]
    if len(uploads) > 50:
        lines.append(f"  … and {len(uploads) - 50} more")
    return "\n".join(lines)


def make_remote_dir(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Creating directories", "allow_write")
    path = str(args.get("path", "")).strip()
    if not path:
        raise ToolError("Say which directory to create (path).")
    handed = _hand_op(profile, "mkdir", path=path)
    if handed is not None:
        return handed
    access.remote(profile).makedirs(path)
    return f"Created {path} (and any missing parents)."


def delete_remote(access: AppAccess, args: dict) -> str:
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Deleting", "allow_delete")
    path = str(args.get("path", "")).strip()
    if not path or path.rstrip("/") in ("", "/"):
        raise ToolError("Refusing: name one file or directory, never the root.")
    # No is_dir: the app stats it on the connection that will do the deleting,
    # rather than this process opening one of its own to answer a question it
    # is about to hand over anyway.
    handed = _hand_op(profile, "delete", path=path)
    if handed is not None:
        return handed
    fs = access.remote(profile)
    stat = fs.stat(path)
    if stat.is_dir and not stat.is_link:
        removed = delete_tree(fs, path)
        if removed == UNCOUNTED:
            # The server did it in one command rather than one per file, so
            # there is no count - which is the point of asking it that way.
            return f"Deleted {path} and everything in it."
        return f"Deleted {path} ({removed} entr(y/ies))."
    fs.remove(path)
    return f"Deleted {path}."


def run_query(access: AppAccess, args: dict) -> str:
    from mysql_runner.db.driver import connect_kwargs, describe_error, import_driver
    from mysql_runner.db.resultformat import format_summary, format_table
    from mysql_runner.db.sqlsplit import split_statements

    profile = access.profile(str(args.get("profile", "")))
    if profile.kind != ConnectionKind.MYSQL:
        mysql_labels = [
            p.label for p in access.profiles() if p.kind == ConnectionKind.MYSQL
        ]
        raise ToolError(
            f"{profile.label} is a {profile.kind.value} profile; queries need "
            "a native MySQL profile. "
            + (f"Available: {', '.join(mysql_labels)}." if mysql_labels else
               "None are stored - add one in the app (kind: MySQL).")
        )
    sql = str(args.get("sql", "")).strip()
    if not sql:
        raise ToolError("Say what to run (sql).")
    statements = split_statements(sql)
    if not statements:
        raise ToolError("No statement found in that SQL.")
    writes = [
        s.sql for s in statements
        if s.sql.lstrip().split(None, 1)[0].casefold() not in READ_ONLY_SQL
    ]
    if writes:
        access.guard(profile, "SQL that changes data", "allow_sql_write")
    database = str(args.get("database", "")).strip()
    if not database:
        # Only when no schema was named. An open console is on whatever
        # schema it is on, and running a statement somewhere other than where
        # the caller asked for it is worse than not running it in the app.
        handed = _hand_op(profile, "query", sql=sql)
        if handed is not None:
            return handed
    database = database or profile.database
    pymysql = import_driver()
    try:
        connection = pymysql.connect(
            **connect_kwargs(
                profile.host, profile.effective_port, profile.username,
                profile.password, database,
            )
        )
    except Exception as exc:
        raise ToolError(describe_error(exc)) from exc
    import time as _time

    blocks: list[str] = []
    try:
        for statement in statements:
            started = _time.perf_counter()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(statement.sql)
                    elapsed = (_time.perf_counter() - started) * 1000
                    if cursor.description:
                        columns = [str(col[0]) for col in cursor.description]
                        rows = cursor.fetchmany(MAX_RESULT_ROWS)
                        more = bool(cursor.fetchone())
                        block = format_table(columns, [tuple(r) for r in rows])
                        block += "\n" + format_summary(len(rows), elapsed, True)
                        if more:
                            block += f"\n(only the first {MAX_RESULT_ROWS} rows are shown)"
                    else:
                        block = format_summary(cursor.rowcount, elapsed, False)
            except Exception as exc:
                block = f"{describe_error(exc)}"
            blocks.append(f"mysql> {statement.sql.strip()}\n{block}")
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return "\n\n".join(blocks)


#: name -> (handler, description, JSON Schema for the arguments).
TOOLS: dict[str, tuple] = {
    "list_profiles": (
        list_profiles,
        "List the stored server profiles Claude may use: label, protocol, "
        "target and environment. Labels are what every other tool's "
        "'profile' argument takes.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "list_remote_dir": (
        list_remote_dir,
        "List a directory on an FTP/FTPS/SFTP server: names, sizes, modified "
        "times. Defaults to the profile's start directory.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "path": {"type": "string", "description": "Remote directory (POSIX style)"},
            },
            "required": ["profile"],
        },
    ),
    "read_remote_file": (
        read_remote_file,
        "Read a text file straight off the server (configs, logs, source). "
        "Refuses binaries and anything over max_bytes.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "path": {"type": "string", "description": "Remote file path"},
                "max_bytes": {"type": "integer", "description": f"Size limit (default {DEFAULT_READ_BYTES}, max {MAX_READ_BYTES})"},
            },
            "required": ["profile", "path"],
        },
    ),
    "download_file": (
        download_file,
        "Download one remote file to a local path.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "remote_path": {"type": "string"},
                "local_path": {"type": "string"},
            },
            "required": ["profile", "remote_path", "local_path"],
        },
    ),
    "upload_file": (
        upload_file,
        "Upload one local file to the server (needs --allow-write). Missing "
        "remote parent directories are created.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "local_path": {"type": "string"},
                "remote_path": {"type": "string", "description": "Full remote path; a trailing / keeps the local name"},
            },
            "required": ["profile", "local_path", "remote_path"],
        },
    ),
    "upload_folder": (
        upload_folder,
        "Upload a local folder's contents into a remote directory, honouring "
        f".deployignore/.gitignore (needs --allow-write; {MAX_FOLDER_FILES} "
        "files per call).",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "local_dir": {"type": "string"},
                "remote_dir": {"type": "string"},
            },
            "required": ["profile", "local_dir", "remote_dir"],
        },
    ),
    "make_remote_dir": (
        make_remote_dir,
        "Create a remote directory, parents included (needs --allow-write).",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["profile", "path"],
        },
    ),
    "delete_remote": (
        delete_remote,
        "Delete one remote file or directory tree (needs --allow-delete).",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["profile", "path"],
        },
    ),
    "run_query": (
        run_query,
        "Run SQL on a native MySQL profile and get mysql-client-style output. "
        "SELECT/SHOW/DESCRIBE/EXPLAIN always work; statements that change "
        "data need --allow-sql-write.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "sql": {"type": "string", "description": "One or more statements, ; separated"},
                "database": {"type": "string", "description": "Schema to use (defaults to the profile's)"},
            },
            "required": ["profile", "sql"],
        },
    ),
}
