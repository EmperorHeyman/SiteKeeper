"""The tools Claude can call, and the guarded access they go through.

Everything here is deliberately headless and Qt-free: the vault unlocks the
way the app does (Windows-sealed key, or the DEK cached in the OS keyring by
a previous unlock - ``SITEKEEPER_MASTER_PASSWORD`` is the fallback for
machines that cache nothing), profiles come from the same encrypted store,
and connections use the same FTP/FTPS/SFTP backends as the file manager.

The safety model is opt-in, and what it reads is the app's own grants file
rather than this process's command line - see ``mcp/policy.py`` for why:

* reading is always allowed - listings with permissions and ownership,
  stats, access reports, file contents, remote search, digests, diffs,
  downloads, SELECTs and the restore-point list,
* changing what is there needs "Upload files and create folders": uploads,
  mkdir, chmod, chown, move, copy, symlink, and putting a file back from a
  restore point,
* deleting anything on a server needs "Delete files and folders",
* SQL that changes data needs "Run SQL that changes data",
* running a command in the server's shell needs "Run commands on the server",
  which is deliberately not part of any of the above: one command can do
  everything they describe and a great deal they do not,
* and none of the above touch a connection marked PRODUCTION until that
  connection is granted production by name.

Each of those is a tick in Sitekeeper's "Connect Claude" window, re-read on
every tool call, so switching one on lands on the next thing Claude tries
rather than the next time the server starts.

Tool output is plain text sized for a model to read, never credentials.

Two rules the descriptions follow, because a tool description is the whole of
the interface a model sees:

* every one of them says what it does to the server - "read only", "merges;
  never deletes", "a directory goes recursively" - since the alternative is a
  caller who cannot rule out the destructive reading and so does the job by
  hand instead,
* and the answers name what happened per file rather than counting, because
  "3 of 17 failed" without a list is a caller who has to go and look anyway.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import replace

from mysql_runner.mcp.policy import LivePolicy, McpPolicy
from mysql_runner.storage.models import ConnectionKind, Environment, ServerProfile
from mysql_runner.transfer import bridge, longlist, remote_exec, shellaccess
from mysql_runner.transfer.base import (
    Capability,
    RemoteFS,
    TransferError,
    Unsupported,
    local_relative,
)
from mysql_runner.transfer import hashing
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.removal import UNCOUNTED, delete_tree

#: In-memory reads are for configs and logs, not for site archives.
MAX_READ_BYTES = 2 * 1024 * 1024
#: What one read brings back when nobody said. Generous on purpose: the old
#: 256 KB was enough to refuse a file the caller had written itself a moment
#: earlier, and "raise max_bytes and call again" is a round trip spent on
#: arithmetic rather than on work.
DEFAULT_READ_BYTES = 1024 * 1024
#: Directories one recursive listing will walk into, and how deep it may go.
#: Mapping a host used to be one call per directory; this is the same walk
#: with the round trips spent by the server instead of by the conversation.
MAX_TREE_DIRS = 400
MAX_TREE_DEPTH = 8
#: Rows one listing prints. A node_modules nobody meant to look at should
#: not fill the answer.
MAX_LISTING_ROWS = 400
#: One tool call pushes at most this many files; more deserves the app.
MAX_FOLDER_FILES = 500
#: How long a command may run before it is given up on, and the ceiling a
#: caller may raise that to. Long enough for composer install on a slow box,
#: short enough that the MCP client is not left holding an open call all day.
DEFAULT_EXEC_TIMEOUT = 120.0
MAX_EXEC_TIMEOUT = 900.0
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
    "allow_exec": "Run commands on the server",
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
        return self._session(profile.id, profile)

    def shell(self, profile: ServerProfile) -> RemoteFS:
        """A connection to this server that can run commands.

        An SFTP session already is one - a command channel is part of SSH, so
        it is the same connection the file tools use. FTP and FTPS have no
        shell at all, and that is a fact about the protocol rather than about
        the machine: the server is nearly always administered over SSH with
        the same account. So their shell is a second connection, an SSH login
        to the same host (see ``transfer/shellaccess.py``), opened once and
        kept beside the first.
        """
        if not profile.kind.is_transfer:
            raise ToolError(
                f"{profile.label} is a {profile.kind.value} profile, so "
                "there is no server to run a command on."
            )
        if profile.kind == ConnectionKind.SFTP:
            fs = self.remote(profile)
            if not fs.supports(Capability.EXEC):
                raise ToolError(
                    f"{profile.label} logs in over SSH, but this account is "
                    "not allowed to run anything - the server refuses to "
                    "open a command channel for it."
                )
            return fs
        # Keyed apart from the file session: the same server, a different
        # login, and closing one must not take the other with it.
        return self._session(profile.id + ":shell", shellaccess.shell_profile(profile))

    def _session(self, key: str, profile: ServerProfile) -> RemoteFS:
        """The live connection under ``key``, opened or reopened as needed."""
        cached = self._remotes.get(key)
        if cached is not None:
            if cached.alive():
                return cached
            try:
                cached.close()
            except Exception:
                pass
            del self._remotes[key]
        fs = self._build(profile)
        fs.connect()
        self._remotes[key] = fs
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
    if op == "exec":
        # The output is the answer, the same way a query's is.
        return f"{detail}\n\n(run on Sitekeeper's own connection to {profile.label})"
    if op == "query":
        # The output is the answer; the note says where it ran.
        return f"{detail}\n\n(run in Sitekeeper's SQL console for {profile.label})"
    return f"{detail} (through Sitekeeper, so it is in this tab's history)"


def _plan_files(
    entries: object, remote_dir: str, base_dir: str
) -> list[tuple[str, str]]:
    """Turn a named list of files into (local, remote) pairs, or refuse.

    An agent that has just edited eleven files across a tree has neither of
    the two shapes the older tools take: one file is ten more round trips,
    and the folder they live in holds three hundred it must not touch. So
    this takes the list it actually has, in whichever form is least work to
    produce:

    * ``"C:/site/app/Model.php"`` - a bare path, landing in ``remote_dir``
      under its own name, or under its path relative to ``base_dir`` when one
      is given, which is what mirrors a subtree in one call;
    * ``{"local": ..., "remote": ...}`` - an explicit destination, absolute
      or relative to ``remote_dir``, for the cases that rename or scatter.

    Everything is decided before a byte moves. A list is an explicit request:
    if one of the paths in it is not there, the honest answer is to say which
    and upload nothing, rather than to send ten of eleven files and leave the
    caller to work out which deploy it now has.
    """
    if not isinstance(entries, list) or not entries:
        raise ToolError(
            "Give 'files' as a list of local paths, or of "
            "{local, remote} objects."
        )
    base = os.path.abspath(base_dir) if base_dir else ""
    pairs: list[tuple[str, str]] = []
    missing: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            local, remote = entry.strip(), ""
        elif isinstance(entry, dict):
            local = str(entry.get("local", "")).strip()
            remote = str(entry.get("remote", "")).strip()
        else:
            raise ToolError(
                f"{entry!r} is neither a path nor a {{local, remote}} object."
            )
        if not local:
            raise ToolError("One of the entries names no local file.")
        local = os.path.abspath(local)
        if not os.path.isfile(local):
            missing.append(local)
            continue
        pairs.append((local, _remote_for(local, remote, remote_dir, base)))
    if len(missing) == 1:
        raise ToolError(
            f"Nothing was uploaded: {missing[0]} is not a file on this machine."
        )
    if missing:
        shown = ", ".join(missing[:10])
        more = f", and {len(missing) - 10} more" if len(missing) > 10 else ""
        raise ToolError(
            f"Nothing was uploaded: {len(missing)} of the paths are not files "
            f"on this machine ({shown}{more})."
        )
    seen: dict[str, str] = {}
    for local, remote in pairs:
        if remote in seen:
            raise ToolError(
                f"Two files are aimed at {remote}: {seen[remote]} and "
                f"{local}. One of them would overwrite the other, so nothing "
                "was uploaded."
            )
        seen[remote] = local
    return pairs


def _remote_for(local: str, remote: str, remote_dir: str, base: str) -> str:
    """Where one file goes, from whichever of the three hints was given."""
    if remote.startswith("/"):
        if remote.endswith("/"):
            return RemoteFS.join(remote, os.path.basename(local))
        return remote
    if not remote_dir:
        raise ToolError(
            "Say where the files go: either 'remote_dir', or an absolute "
            "'remote' on every entry."
        )
    if remote:
        # Relative to remote_dir - the useful reading, and the only one that
        # is not simply wrong for a path with no leading slash.
        return RemoteFS.join(remote_dir, remote.rstrip("/"))
    if base:
        rel = local_relative(base, local)
        if rel.startswith("../") or rel == "..":
            raise ToolError(
                f"{local} is not inside base_dir, so there is no relative "
                "path to keep. Name its 'remote', or drop base_dir."
            )
        return RemoteFS.join(remote_dir, rel)
    return RemoteFS.join(remote_dir, os.path.basename(local))


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
        warning = label_mismatch(p)
        if warning:
            lines.append(f"  ⚠ {warning}")
    lines.append("")
    lines.append(f"Claude is {policy.describe()}. Change it in {WINDOW}.")
    return "\n".join(lines)


#: A host or an account named inside a label: "rapl-group@webhosting.cz",
#: "sftp.example.com", "user__ftp". Anything shorter or vaguer than this is
#: a name rather than a claim about where the connection goes.
_LABEL_TARGET = re.compile(r"[A-Za-z0-9_.-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}")


def label_mismatch(profile: ServerProfile) -> str:
    """A warning when the label names a target the connection does not use.

    Twenty-one saved connections with write and delete granted is a room
    full of foot-guns if one of them is called ``emproduction__ftp@sftp.host``
    and actually logs in as ``kiosekunemlyna__1``. The label is what a caller
    reads and quotes; it is the only thing they have, so when it makes a
    claim about the destination and the claim is false, that is worth a line
    of its own.

    Deliberately quiet unless the label really does name something: a label
    has to contain an ``account@host`` or a dotted host name before anything
    is compared, so "Live site" and "Client A - staging" never warn.
    """
    label = profile.label or ""
    claim = _LABEL_TARGET.search(label)
    if claim is None:
        return ""
    claimed = claim.group(0).casefold()
    account, _, host = claimed.rpartition("@")
    real_host = (profile.host or "").casefold()
    real_user = (profile.username or "").casefold()
    if profile.kind == ConnectionKind.PHPMYADMIN:
        real_host = (profile.url or "").casefold()
    problems = []
    if host and real_host and host not in real_host and real_host not in host:
        problems.append(f"host {host} but connects to {profile.host or profile.url}")
    if account and real_user and account != real_user:
        problems.append(f"user {account} but logs in as {profile.username}")
    if not problems:
        return ""
    return "the label says " + ", and ".join(problems)


def list_remote_dir(access: AppAccess, args: dict) -> str:
    """List one directory - or a whole subtree - with mode and ownership."""
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    path = str(args.get("path", "")).strip() or profile.remote_dir.strip() or fs.home()
    depth = max(1, min(int(args.get("depth", 1) or 1), MAX_TREE_DEPTH))
    detail = args.get("details", True) is not False
    if depth > 1:
        return _list_tree(fs, path, depth, detail)
    entries = _detailed(fs, path, detail)
    lines = [f"{path} — {len(entries)} entr(y/ies):", _listing_header(entries)]
    lines += [_listing_row(entry) for entry in entries[:MAX_LISTING_ROWS]]
    if len(entries) > MAX_LISTING_ROWS:
        lines.append(f"… and {len(entries) - MAX_LISTING_ROWS} more, not listed")
    return "\n".join(line for line in lines if line)


def _detailed(fs: RemoteFS, path: str, detail: bool) -> list:
    """The listing, with owner and group as names where that is possible.

    SFTP reports ownership as numbers, and "33:33" is not the answer to a
    permissions question. One ``ls -lA`` on the same connection turns those
    into ``www-data:www-data``; where there is no shell, the numbers - or
    whatever an FTP server volunteered - stand as they are.
    """
    entries = fs.listdir(path)
    if not detail:
        return entries
    named = remote_exec.named_listing(fs, path)
    if not named:
        return entries
    merged = []
    for entry in entries:
        extra = named.get(entry.name)
        if extra is None or not extra.owner:
            merged.append(entry)
            continue
        merged.append(
            replace(
                entry,
                owner=extra.owner,
                group=extra.group,
                mode=entry.mode if entry.mode is not None else extra.mode,
            )
        )
    return merged


def _listing_header(entries: list) -> str:
    if not any(entry.mode is not None or entry.owner for entry in entries):
        return ""
    return (
        f"{'permissions':<11} {'mode':<5} {'owner:group':<22} "
        f"{'size':>9}  {'modified':<19} name"
    )


def _listing_row(entry) -> str:
    kind = "d" if entry.is_dir else ("l" if entry.is_link else "-")
    letters = longlist.mode_letters(entry.mode) or "?????????"
    octal = f"{entry.mode & 0o7777:04o}" if entry.mode is not None else "????"
    who = f"{entry.owner}:{entry.group}" if entry.owner else "?"
    size = "" if entry.is_dir else _human_size(entry.size)
    name = entry.name
    if entry.is_link and entry.link_target:
        name += f" -> {entry.link_target}"
    return (
        f"{kind}{letters:<10} {octal:<5} {who:<22} {size:>9}  "
        f"{_human_time(entry.modified):<19} {name}"
    )


def _list_tree(fs: RemoteFS, root: str, depth: int, detail: bool) -> str:
    """Walk ``depth`` levels down, one section per directory.

    Breadth first and bounded, so a deep tree comes back shallow-but-complete
    rather than as one branch and a truncation notice.
    """
    queue = [(root, 0)]
    seen = 0
    lines = []
    truncated = False
    while queue:
        current, level = queue.pop(0)
        if seen >= MAX_TREE_DIRS:
            truncated = True
            break
        seen += 1
        try:
            entries = _detailed(fs, current, detail)
        except TransferError as exc:
            lines.append(f"{current} — not listed: {exc}")
            continue
        lines.append(f"{current} — {len(entries)} entr(y/ies):")
        lines += [_listing_row(entry) for entry in entries[:MAX_LISTING_ROWS]]
        if len(entries) > MAX_LISTING_ROWS:
            lines.append(f"… and {len(entries) - MAX_LISTING_ROWS} more, not listed")
        lines.append("")
        if level + 1 < depth:
            queue += [
                (RemoteFS.join(current, entry.name), level + 1)
                for entry in entries
                if entry.is_dir and not entry.is_link
            ]
    if truncated:
        lines.append(
            f"Stopped after {MAX_TREE_DIRS} directories. Narrow the path or "
            "the depth for the rest."
        )
    return "\n".join(lines).strip()


def stat_remote(access: AppAccess, args: dict) -> str:
    """What each of these paths is: type, size, mode, ownership, link target."""
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    paths = _path_list(args, profile)
    lines = []
    for path in paths:
        lines.append(_describe_path(fs, path))
    return "\n".join(lines)


def _path_list(args: dict, profile) -> list[str]:
    """The paths a tool was asked about: 'paths', or 'path', in that order."""
    raw = args.get("paths")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        single = str(args.get("path", "")).strip()
        raw = [single] if single else []
    paths = [str(item).strip() for item in raw if str(item).strip()]
    if not paths:
        raise ToolError("Say which remote path(s) to look at.")
    if len(paths) > remote_exec.MAX_PROBE_PATHS:
        raise ToolError(
            f"That is {len(paths)} paths; one call looks at at most "
            f"{remote_exec.MAX_PROBE_PATHS}."
        )
    return paths


def _describe_path(fs: RemoteFS, path: str) -> str:
    """One path in one line, with whatever the server will say about it."""
    try:
        info = fs.stat(path)
    except TransferError as exc:
        return f"{path}: not there ({exc})"
    kind = "directory" if info.is_dir else "file"
    if info.is_link:
        kind = "symlink"
    bits = [kind]
    if not info.is_dir:
        bits.append(_human_size(info.size))
    mode = longlist.describe_mode(info.mode)
    if mode:
        bits.append(mode)
    owner = _named_owner(fs, path, info)
    if owner:
        bits.append(owner)
    if info.modified:
        bits.append(f"modified {_human_time(info.modified)}")
    if info.is_link and info.link_target:
        bits.append(f"-> {info.link_target}")
    return f"{path}: " + ", ".join(bits)


def _named_owner(fs: RemoteFS, path: str, info) -> str:
    """owner:group, as names when a shell can name them."""
    owner, group = info.owner, info.group
    if owner.isdigit() or not owner:
        facts = _one_probe(fs, path)
        if facts is not None and facts.owner:
            owner, group = facts.owner, facts.group
    if not owner:
        return ""
    return f"{owner}:{group}" if group else owner


def _one_probe(fs: RemoteFS, path: str):
    """``ls -ld`` for one path, or None when there is no shell to ask."""
    if not fs.supports(Capability.EXEC):
        return None
    try:
        return remote_exec.path_facts(fs, [path]).get(path)
    except TransferError:
        return None


def read_remote_file(access: AppAccess, args: dict) -> str:
    """Read a remote text file - all of it, its end, or a window of it."""
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    path = str(args.get("path", "")).strip()
    if not path:
        raise ToolError("Say which remote file to read (path).")
    cap = min(
        int(args.get("max_bytes", DEFAULT_READ_BYTES) or DEFAULT_READ_BYTES),
        MAX_READ_BYTES,
    )
    tail_lines = max(0, int(args.get("tail_lines", 0) or 0))
    offset = max(0, int(args.get("offset", 0) or 0))
    info = fs.stat(path)
    if info.is_dir:
        raise ToolError(f"{path} is a directory; use list_remote_dir.")

    if tail_lines:
        # The end of the file, sized to the cap: a 200 MB log answers as
        # fast as a small one, because only the window crosses the wire.
        offset = max(0, info.size - cap)
        data = fs.read_range(path, offset, 0)
        note = _tail_note(path, info.size, offset)
    elif offset:
        data = fs.read_range(path, offset, cap)
        note = (
            f"{path}: {_human_size(len(data))} from byte {offset} of "
            f"{_human_size(info.size)}."
        )
    else:
        if info.size > cap:
            raise ToolError(
                f"{path} is {_human_size(info.size)}, over this call's "
                f"{_human_size(cap)} limit. Raise max_bytes (up to "
                f"{_human_size(MAX_READ_BYTES)}), read the end with "
                "tail_lines, a window with offset, or use download_file."
            )
        buffer = io.BytesIO()
        fs.stream_download(path, buffer.write)
        data = buffer.getvalue()
        note = ""

    if b"\x00" in data:
        return (
            f"{path} is binary ({_human_size(info.size)}); use download_file "
            "to fetch it."
        )
    text = data.decode("utf-8", errors="replace")
    if tail_lines:
        lines = text.splitlines()
        if offset and lines:
            # The window almost certainly began mid-line; that fragment is
            # not a line of the file and must not be reported as one.
            lines = lines[1:]
        text = "\n".join(lines[-tail_lines:])
    return f"{note}\n{text}" if note else text


def _tail_note(path: str, size: int, offset: int) -> str:
    if not offset:
        return f"{path}: the whole file ({_human_size(size)})."
    return (
        f"{path}: the last {_human_size(size - offset)} of "
        f"{_human_size(size)} (from byte {offset})."
    )


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
    """Push a local directory's contents into a remote one.

    Merges: a file on the server that is not in the local folder is left
    exactly where it is. Nothing here deletes anything - which is the
    question anybody about to run this against a live docroot has to have
    answered before they dare, and the reason the answer is now in the tool
    description as well as here.
    """
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
    return _upload_batch(
        access,
        profile,
        args,
        uploads,
        remote_dir,
        directories=[remote_dir, *directories],
    )

#: What a batch is about to do to each file it names.
NEW, OVERWRITE, IDENTICAL = "new", "overwrite", "identical"


def _classify(fs: RemoteFS, uploads: list, *, digests: bool) -> dict[str, str]:
    """For each destination: new, overwrite, or byte-for-byte identical.

    This is what makes a push to a live docroot reviewable before it happens.
    Sizes and times alone would call a file "changed" whenever it had been
    re-saved, so identity is decided by digest - which the server computes for
    every file in one command where it has a shell, and which is worth a read
    per file where it does not.
    """
    verdicts: dict[str, str] = {}
    unsure: list[tuple[str, str]] = []
    for local, remote in uploads:
        try:
            info = fs.stat(remote)
        except TransferError:
            verdicts[remote] = NEW
            continue
        if info.is_dir:
            verdicts[remote] = OVERWRITE  # refused later, by the upload itself
            continue
        if not digests:
            verdicts[remote] = OVERWRITE
            continue
        if info.size != _local_size(local):
            verdicts[remote] = OVERWRITE
            continue
        unsure.append((local, remote))
    if unsure:
        verdicts.update(_by_digest(fs, unsure))
    return verdicts


def _by_digest(fs: RemoteFS, pairs: list) -> dict[str, str]:
    """Same-size files: identical or not, decided by hash."""
    verdicts: dict[str, str] = {}
    for local, remote in pairs:
        try:
            here = hashing.hash_local_file(local)
            there = hashing.hash_remote_file(fs, remote)
        except (TransferError, OSError):
            verdicts[remote] = OVERWRITE  # cannot prove identity: assume not
            continue
        verdicts[remote] = IDENTICAL if here and here == there else OVERWRITE
    return verdicts


def _local_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def _plan_report(profile, uploads: list, verdicts: dict, remote_dir: str) -> str:
    """The dry run: what each named file would do, and nothing done."""
    counts = {NEW: 0, OVERWRITE: 0, IDENTICAL: 0}
    rows = []
    for _local, remote in uploads:
        verdict = verdicts.get(remote, OVERWRITE)
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append(f"  {verdict.upper():<9} {remote}")
    head = (
        f"Dry run on {profile.label}"
        + (f" ({remote_dir})" if remote_dir else "")
        + f": {len(uploads)} file(s) - {counts[NEW]} new, "
        f"{counts[OVERWRITE]} would be overwritten, {counts[IDENTICAL]} "
        "already identical."
    )
    tail = (
        "Nothing was uploaded. Call again without dry_run to send it; add "
        "skip_identical to leave the identical ones alone."
    )
    return "\n".join([head, *rows[:MAX_LISTING_ROWS], "", tail])


def upload_files(access: AppAccess, args: dict) -> str:
    """Upload a named list of files in one call, as one queue.

    No ignore rules, deliberately - unlike :func:`upload_folder`, which walks
    a tree and must not drag ``node_modules`` or a local ``.env`` into it.
    Here every file was named, and naming a file *is* the decision; filtering
    one back out again would mean an upload that reported success and moved
    nothing, which is the same reasoning the app applies to a file somebody
    selected and pressed Upload on.
    """
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Uploading", "allow_write")
    remote_dir = str(args.get("remote_dir", "")).strip()
    base_dir = str(args.get("base_dir", "")).strip()
    uploads = _plan_files(args.get("files"), remote_dir, base_dir)
    if len(uploads) > MAX_FOLDER_FILES:
        raise ToolError(
            f"That is {len(uploads)} files; one call carries at most "
            f"{MAX_FOLDER_FILES}. Send them in batches, or use the app for "
            "whole-site deploys."
        )
    return _upload_batch(access, profile, args, uploads, remote_dir)


def _upload_batch(
    access: AppAccess,
    profile,
    args: dict,
    uploads: list,
    remote_dir: str,
    directories: list | tuple = (),
) -> str:
    """The shared middle of both batch tools: preview, skip, send, report."""
    dry_run = bool(args.get("dry_run"))
    skip_identical = bool(args.get("skip_identical"))
    verdicts: dict = {}
    skipped: list = []
    if dry_run or skip_identical:
        verdicts = _classify(access.remote(profile), uploads, digests=True)
        if dry_run:
            return _plan_report(profile, uploads, verdicts, remote_dir)
        skipped = [
            remote for _l, remote in uploads if verdicts.get(remote) == IDENTICAL
        ]
        uploads = [pair for pair in uploads if verdicts.get(pair[1]) != IDENTICAL]
        if not uploads:
            return (
                f"Nothing to do: all {len(skipped)} file(s) on {profile.label} "
                "are already byte-for-byte identical, so nothing was sent."
            )
    note = f"{len(uploads)} file(s)"
    if remote_dir:
        note += f" -> {remote_dir}"
    report = _hand_over(profile, uploads, note)
    if report is None:
        report = _upload_directly(access, profile, uploads, verdicts, directories)
    if skipped:
        report += (
            f"\nSkipped {len(skipped)} file(s) already identical on the "
            "server: " + ", ".join(skipped[:10])
            + (f", and {len(skipped) - 10} more" if len(skipped) > 10 else "")
        )
    return report

def _upload_directly(
    access: AppAccess, profile, uploads, verdicts=None, directories=()
) -> str:
    """Send the pairs from this process, when there is no app to hand them to.

    Parents are created in one call rather than one per file: on a server with
    a shell that is a single ``mkdir -p``, and it is the difference between a
    batch of forty files costing forty round trips and costing one.
    """
    fs = access.remote(profile)
    # ``directories`` carries the ones a folder push has to create even though
    # no file in this batch lands in them: an empty cache/ that the
    # application expects to find is part of the tree it was told to send.
    parents = sorted(
        ({RemoteFS.parent(remote) for _local, remote in uploads} | set(directories))
        - {"", "/"}
    )
    if parents:
        fs.makedirs_many(parents)
    arrived: list[str] = []
    moved = 0
    failures: list[str] = []
    for local, remote in uploads:
        # Measured before the upload rather than after it: the file is known
        # to be there (_plan_files checked), and a size read that fails on the
        # way out must not be able to turn a successful upload into a failed
        # one in the report.
        try:
            size = os.path.getsize(local)
        except OSError:
            size = 0
        try:
            fs.upload(local, remote)
            _preserve_mtime(fs, local, remote)
        except TransferError as exc:
            failures.append(f"{remote}: {exc}")
            if len(failures) >= 5:
                failures.append("… stopping after 5 failures.")
                break
            continue
        arrived.append(remote)
        moved += size
    lines = [
        f"Uploaded {len(arrived)}/{len(uploads)} file(s) to {profile.label} "
        f"({_human_size(moved)})."
    ]
    lines += [f"FAILED {line}" for line in failures]
    # Per file, and what it did to what was there: one failure in seventeen
    # must not leave the caller guessing which sixteen went.
    marks = verdicts or {}
    lines += [
        f"  {marks.get(remote, 'sent').upper():<9} {remote}"
        for remote in arrived[:MAX_LISTING_ROWS]
    ]
    if len(arrived) > MAX_LISTING_ROWS:
        lines.append(f"  … and {len(arrived) - MAX_LISTING_ROWS} more")
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


def search_remote(access: AppAccess, args: dict) -> str:
    """grep -rn on the server: the companion to read_remote_file.

    The alternative, which is what happens without this, is downloading a
    codebase to search it and then having to prove that what you searched is
    what is running.
    """
    profile = access.profile(str(args.get("profile", "")))
    pattern = str(args.get("pattern", ""))
    if not pattern.strip():
        raise ToolError("Say what to search for (pattern).")
    root = str(args.get("path", "")).strip() or profile.remote_dir.strip() or "."
    fs = access.shell(profile)
    limit = max(1, min(int(args.get("limit", 200) or 200), remote_exec.GREP_LIMIT))
    try:
        outcome = remote_exec.grep(
            fs,
            root,
            pattern,
            fixed=args.get("regex") is not True,
            ignore_case=bool(args.get("ignore_case")),
            include=str(args.get("include", "")).strip(),
            limit=limit,
        )
    except Unsupported as exc:
        raise ToolError(str(exc)) from exc
    except TransferError as exc:
        raise ToolError(f"{profile.label}: {exc}") from exc
    if outcome.error:
        raise ToolError(f"{profile.label}: {outcome.error}")
    if not outcome.hits:
        return f"No matches for {pattern!r} under {root} on {profile.label}."
    lines = [
        f"{len(outcome.hits)} match(es) for {pattern!r} under {root} "
        f"({outcome.tool}):"
    ]
    lines += [f"{hit.path}:{hit.line}: {hit.text.strip()}" for hit in outcome.hits]
    if outcome.truncated:
        lines.append(f"… stopped at {limit} matches; narrow the pattern or the path.")
    return "\n".join(lines)


#: Accounts a web server usually runs as, tried in order when nobody says
#: which user to ask about. The first one the server actually has is used.
WEB_USERS = ("www-data", "apache", "nginx", "http", "httpd", "daemon")


def check_remote_access(access: AppAccess, args: dict) -> str:
    """Whether a given user can read, write and traverse these paths.

    The question this exists for is "why can PHP not write here", and it used
    to be unanswerable through this server: a listing gave name, size and
    time, so the only way to find out was to deploy something and watch what
    happened. Everything needed is on the server and cheap to read - the
    mode, the owner, the group, the asked-about account's group memberships -
    so the answer is arithmetic once they are all in one place.

    Ancestors are included because the second cause, after the directory's
    own mode, is a parent nobody looked at: a 0777 uploads directory inside a
    0700 one is not writable by anybody but its owner, and nothing about the
    directory itself says so.
    """
    profile = access.profile(str(args.get("profile", "")))
    fs = access.shell(profile)
    paths = _path_list(args, profile)
    subject = str(args.get("user", "")).strip()
    me = remote_exec.effective_user(fs)
    if not subject:
        subject = _first_web_user(fs) or me
    user = remote_exec.user_facts(fs, subject)

    wanted: list[str] = []
    for path in paths:
        wanted += [item for item in remote_exec.ancestors(path) if item not in wanted]
        if path not in wanted:
            wanted.append(path)
    if len(wanted) > remote_exec.MAX_PROBE_PATHS:
        raise ToolError(
            f"Those {len(paths)} path(s) and their parents come to "
            f"{len(wanted)} directories, over the {remote_exec.MAX_PROBE_PATHS} "
            "one call looks at. Ask about fewer at a time."
        )
    try:
        facts = remote_exec.path_facts(fs, wanted)
    except TransferError as exc:
        raise ToolError(f"{profile.label}: {exc}") from exc

    lines = [
        f"{profile.label}: logged in as {me or 'unknown'}; asking about "
        f"{user.name or 'unknown'}"
        + (
            f" (groups: {', '.join(user.groups)})"
            if user.groups
            else " (no such account on this server)" if user.name and not user.known else ""
        )
    ]
    for path in paths:
        lines.append("")
        lines += _access_lines(path, facts, user)
    return "\n".join(lines)


def _first_web_user(fs: RemoteFS) -> str:
    """The first of the usual web-server accounts this server actually has."""
    for candidate in WEB_USERS:
        if remote_exec.user_facts(fs, candidate).known:
            return candidate
    return ""


def _access_lines(path: str, facts: dict, user) -> list[str]:
    """One path: what it is, what the user may do, and what is in the way."""
    own = facts.get(path)
    if own is None or not own.exists:
        reason = own.error if own is not None and own.error else "not found"
        lines = [f"{path}: does not exist ({reason})"]
        parent = RemoteFS.parent(path)
        parent_facts = facts.get(parent)
        if parent_facts is not None and parent_facts.exists:
            bits, why = remote_exec.permission_bits(parent_facts, user)
            can = "may" if "w" in bits else "may NOT"
            lines.append(
                f"  its parent {parent} is {longlist.describe_mode(parent_facts.mode)} "
                f"{parent_facts.owner}:{parent_facts.group}, so {user.name} "
                f"{can} create it ({why})"
            )
        return lines

    bits, why = remote_exec.permission_bits(own, user)
    detail = (
        f"{own.kind}, {longlist.describe_mode(own.mode)}, "
        f"{own.owner}:{own.group}"
    )
    if own.entry is not None and own.entry.is_link and own.entry.link_target:
        detail += f", -> {own.entry.link_target}"
    lines = [f"{path}: {detail}"]
    if not bits:
        lines.append(f"  {user.name}: cannot be worked out - {why}")
        return lines
    verdict = ", ".join(
        f"{name} {'yes' if flag in bits else 'NO'}"
        for name, flag in (("read", "r"), ("write", "w"), (
            "traverse" if own.entry is not None and own.entry.is_dir else "execute",
            "x",
        ))
    )
    lines.append(f"  {user.name}: {verdict} — it {why}")

    blocked = _blocked_ancestor(path, facts, user)
    if blocked:
        lines.append(
            f"  but {blocked[0]} is {blocked[1]}, which {user.name} cannot "
            "enter, so nothing inside it is reachable whatever its own mode says"
        )
    elif "w" not in bits and own.mode is not None:
        lines.append(f"  {_write_advice(own, user)}")
    return lines


def _blocked_ancestor(path: str, facts: dict, user) -> tuple[str, str] | None:
    """The first directory on the way in that the user cannot traverse."""
    for ancestor in remote_exec.ancestors(path):
        item = facts.get(ancestor)
        if item is None or not item.exists:
            continue
        bits, _why = remote_exec.permission_bits(item, user)
        if bits and "x" not in bits:
            return (
                ancestor,
                f"{longlist.describe_mode(item.mode)} {item.owner}:{item.group}",
            )
    return None


def _write_advice(facts, user) -> str:
    """The smallest change that would let this user write. Facts, not orders."""
    if user.name == facts.owner:
        return (
            f"to let {user.name} write: chmod u+w (it owns this, so the owner "
            "bits are the ones that count)"
        )
    if facts.group and facts.group in user.groups:
        return (
            f"to let {user.name} write: chmod g+w ({user.name} is in "
            f"{facts.group}, so the group bits are the ones that count)"
        )
    return (
        f"to let {user.name} write, either give it the group - chown "
        f":{user.name} plus chmod g+w - or make it the owner. Widening the "
        "'other' bits with chmod o+w would work and would also let every "
        "account on the server write here"
    )


def diff_remote(access: AppAccess, args: dict) -> str:
    """Is what is on the server what is on this machine? By digest, not by date.

    The check worth making before overwriting a production docroot, and it
    used to take a download and a local diff to make - which is enough work
    that it gets skipped, which is how a deploy overwrites somebody's hotfix.
    """
    profile = access.profile(str(args.get("profile", "")))
    fs = access.remote(profile)
    local = str(args.get("local_path", "")).strip()
    remote = str(args.get("remote_path", "")).strip()
    if not local or not remote:
        raise ToolError("Both local_path and remote_path are required.")
    if os.path.isfile(local):
        return _diff_one_file(fs, profile, local, remote)
    if not os.path.isdir(local):
        raise ToolError(f"{local} is not a file or directory on this machine.")
    return _diff_tree(fs, profile, local, remote, bool(args.get("ignore_rules", True)))


def _diff_one_file(fs: RemoteFS, profile, local: str, remote: str) -> str:
    try:
        info = fs.stat(remote)
    except TransferError as exc:
        return f"{remote} is not on {profile.label} ({exc}); {local} is only here."
    if info.is_dir:
        raise ToolError(f"{remote} is a directory on the server and {local} is a file.")
    here = hashing.hash_local_file(local)
    there = hashing.hash_remote_file(fs, remote)
    if here and here == there:
        return (
            f"Identical: {local} and {remote} are the same "
            f"{_human_size(info.size)} of bytes (sha256 {here[:16]}…)."
        )
    if not there:
        return (
            f"Cannot tell: {profile.label} would not produce a digest for "
            f"{remote}. Sizes are {_human_size(_local_size(local))} here and "
            f"{_human_size(info.size)} there."
        )
    return (
        f"Different: {local} is {_human_size(_local_size(local))} "
        f"(sha256 {here[:16]}…), {remote} is {_human_size(info.size)} "
        f"(sha256 {there[:16]}…)."
    )


def _diff_tree(fs: RemoteFS, profile, local: str, remote: str, use_rules: bool) -> str:
    rules = (
        IgnoreRules.from_local_dir(local, with_defaults=True)
        if use_rules
        else IgnoreRules.empty()
    )
    try:
        here = hashing.snapshot_local(local, rules=rules)
        there = hashing.snapshot_remote(fs, remote, rules=rules)
    except TransferError as exc:
        raise ToolError(f"{profile.label}: {exc}") from exc
    report = hashing.compare(here, there)
    lines = [
        f"{local} vs {remote} on {profile.label}, compared by "
        f"{report.compared_by}: {report.summary()}."
    ]
    for status, label in (
        (hashing.DiffStatus.DIFFERENT, "differs"),
        (hashing.DiffStatus.LOCAL_ONLY, "only here"),
        (hashing.DiffStatus.REMOTE_ONLY, "only on the server"),
        (hashing.DiffStatus.UNKNOWN, "not comparable"),
    ):
        paths = report.paths(status)
        if not paths:
            continue
        lines.append("")
        lines.append(f"{label} ({len(paths)}):")
        lines += [f"  {item}" for item in paths[:MAX_LISTING_ROWS]]
        if len(paths) > MAX_LISTING_ROWS:
            lines.append(f"  … and {len(paths) - MAX_LISTING_ROWS} more")
    # A file that could not be hashed is not the same as a file that matched,
    # and a comparison that quietly leaves it out is the kind of answer that
    # gets trusted and should not be.
    problems = list(there.errors or []) + list(here.errors or [])
    if problems:
        lines.append("")
        lines.append(f"could not be compared ({len(problems)}):")
        lines += [f"  {item}" for item in problems[:20]]
        if len(problems) > 20:
            lines.append(f"  … and {len(problems) - 20} more")
    return "\n".join(lines)


# ----- what this app has already undone, and can undo again ---------------
def list_undo_history(access: AppAccess, args: dict) -> str:
    """The overwrites and deletions this app kept a copy of, newest first.

    Every write through Sitekeeper says it can be undone, and until now
    nothing could see or use those restore points from here - a safety net
    that only the person at the keyboard could reach. It is the same journal
    the app's History window shows, read from the same place on disk.
    """
    store = _history_store()
    profile = None
    ref = str(args.get("profile", "")).strip()
    if ref:
        profile = access.profile(ref)
    limit = max(1, min(int(args.get("limit", 30) or 30), 200))
    entries = store.entries(profile_id=profile.id if profile else "", limit=limit)
    if not entries:
        where = f" for {profile.label}" if profile else ""
        return (
            f"No restore points{where}. Sitekeeper keeps one for every file "
            "it overwrites or deletes; nothing here has done either yet."
        )
    lines = [f"{len(entries)} restore point(s), newest first:"]
    for entry in entries:
        state = "undoable" if entry.can_undo else "no backup"
        where = entry.profile_label or "this machine"
        lines.append(
            f"  {entry.id}  {_human_time(entry.when)}  "
            f"{entry.action.value:<15} {state:<9}  {where}: {entry.target}"
        )
    lines.append("")
    lines.append(
        "Put one back with undo_remote_change and its id. The copy is this "
        "machine's, so a restore sends those bytes back to the server."
    )
    return "\n".join(lines)


def undo_remote_change(access: AppAccess, args: dict) -> str:
    """Put one file back the way it was before this app changed it."""
    entry_id = str(args.get("entry_id", "")).strip()
    if not entry_id:
        raise ToolError(
            "Say which restore point (entry_id) - list_undo_history has them."
        )
    store = _history_store()
    entry = next((item for item in store.load() if item.id == entry_id), None)
    if entry is None:
        raise ToolError(f"No restore point with id {entry_id!r}.")
    if not entry.can_undo:
        raise ToolError(
            f"{entry.describe()} has no kept copy, so there is nothing to put "
            "back. (Sitekeeper keeps one for overwrites and deletions it did "
            "itself, within the size limit set in Settings.)"
        )
    profile = None
    if entry.profile_id:
        profile = next(
            (item for item in access.profiles() if item.id == entry.profile_id), None
        )
    if entry.is_remote:
        if profile is None:
            raise ToolError(
                "That restore point belongs to a connection this server "
                "cannot see. Check the scope in " + WINDOW + "."
            )
        access.guard(profile, "Restoring a file on the server", "allow_write")
        fs = access.remote(profile)
    else:
        fs = None
    try:
        message = store.undo(entry_id, fs)
    except TransferError as exc:
        raise ToolError(f"{entry.describe()}: {exc}") from exc
    except OSError as exc:
        raise ToolError(f"{entry.describe()}: {exc}") from exc
    return message


def _history_store():
    """The app's own journal, at the path the app keeps it."""
    from mysql_runner.transfer.history import HistoryStore

    return HistoryStore()


# ----- changing what is already there -------------------------------------
# These need "Upload files and create folders" rather than a permission of
# their own. The grant is about whether Claude may change this server at all,
# and a chmod or a move is a change of the same kind and the same weight as
# writing a file; a fifth checkbox for each verb would be a longer list that
# said no more. Deleting keeps its own, because it is the one that loses data.
def chmod_remote(access: AppAccess, args: dict) -> str:
    """Set permissions on a remote path, optionally through a tree."""
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Changing permissions", "allow_write")
    path = str(args.get("path", "")).strip()
    if not path:
        raise ToolError("Say which remote path to change (path).")
    mode = _octal_mode(args.get("mode"))
    recursive = bool(args.get("recursive"))
    scope = str(args.get("scope", "all")).strip().lower() or "all"
    if scope not in ("all", "files", "dirs"):
        raise ToolError("scope is 'all', 'files' or 'dirs'.")
    fs = access.remote(profile)
    shown = format(mode, "04o")
    if recursive:
        try:
            remote_exec.chmod_tree(access.shell(profile), path, mode, scope=scope)
        except Unsupported as exc:
            raise ToolError(str(exc)) from exc
        except TransferError as exc:
            raise ToolError(f"{profile.label}: {exc}") from exc
        which = {"all": "everything", "files": "the files", "dirs": "the directories"}
        return f"Set {shown} on {which[scope]} under {path}."
    if not fs.supports(Capability.CHMOD):
        raise ToolError(
            f"{profile.label} cannot change permissions: this server does not "
            "offer it (an FTP server has to advertise SITE CHMOD)."
        )
    try:
        fs.chmod(path, mode)
    except TransferError as exc:
        raise ToolError(f"{path}: {exc}") from exc
    return f"Set {shown} on {path}."


def chown_remote(access: AppAccess, args: dict) -> str:
    """Change owner and/or group on the server. Needs a shell."""
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Changing ownership", "allow_write")
    path = str(args.get("path", "")).strip()
    if not path:
        raise ToolError("Say which remote path to change (path).")
    owner = str(args.get("owner", "")).strip()
    group = str(args.get("group", "")).strip()
    if not owner and not group:
        raise ToolError("Say the new owner, the new group, or both.")
    try:
        remote_exec.chown_tree(
            access.shell(profile),
            path,
            owner,
            group,
            recursive=bool(args.get("recursive")),
        )
    except Unsupported as exc:
        raise ToolError(str(exc)) from exc
    except TransferError as exc:
        # Almost always "Operation not permitted": chown is root's, and an
        # unprivileged account can give a file away to a group it belongs to
        # at most. Worth saying, because the next thing tried should be a
        # chmod rather than the same command again.
        raise ToolError(
            f"{path}: {exc}. Changing an owner needs root on most servers; "
            "a group change needs you to be in the target group."
        ) from exc
    spec = f"{owner}:{group}" if owner and group else (owner or f":{group}")
    where = " and everything under it" if args.get("recursive") else ""
    return f"{path}{where} now belongs to {spec}."


def move_remote(access: AppAccess, args: dict) -> str:
    """Move or rename on the server, without the bytes leaving it."""
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Moving files", "allow_write")
    source, target = _two_paths(args)
    fs = access.remote(profile)
    target = _resolve_target(fs, source, target)
    try:
        fs.rename(source, target)
        return f"Moved {source} -> {target}."
    except TransferError as first:
        # A rename cannot cross filesystems, and a docroot with the uploads
        # directory on its own volume is an ordinary thing. mv copies and
        # deletes in that case, still entirely on the server.
        if not fs.supports(Capability.EXEC):
            raise ToolError(f"{source}: {first}") from first
    try:
        remote_exec.move_path(access.shell(profile), source, target)
    except TransferError as exc:
        raise ToolError(f"{source}: {exc}") from exc
    return f"Moved {source} -> {target} (with mv, so across filesystems)."


def copy_remote(access: AppAccess, args: dict) -> str:
    """Copy on the server. 22 MB moved two directories should cost nothing."""
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Copying files", "allow_write")
    source, target = _two_paths(args)
    fs = access.remote(profile)
    target = _resolve_target(fs, source, target)
    if fs.supports(Capability.EXEC):
        try:
            remote_exec.copy_tree(fs, source, target)
        except TransferError as exc:
            raise ToolError(f"{source}: {exc}") from exc
        return f"Copied {source} -> {target} on the server."
    return _copy_through_here(fs, source, target)


def _copy_through_here(fs: RemoteFS, source: str, target: str) -> str:
    """Copy one file by way of this machine, because FTP cannot copy at all.

    Said plainly in the answer rather than hidden: the bytes make a round
    trip, which on a large file is the thing the caller was trying to avoid,
    and knowing that is what lets them decide to do it another way.
    """
    info = fs.stat(source)
    if info.is_dir:
        raise ToolError(
            f"{source} is a directory, and this connection has no shell to "
            "copy it with. FTP has no copy command, so a directory would "
            "have to be fetched and sent back file by file - do that with "
            "download_file and upload_files if it is really what you want."
        )
    handle = tempfile.NamedTemporaryFile(prefix="sk-copy-", delete=False)
    scratch = handle.name
    handle.close()
    try:
        fs.download(source, scratch)
        parent = RemoteFS.parent(target)
        if parent not in ("", "/"):
            fs.makedirs(parent)
        fs.upload(scratch, target)
    except TransferError as exc:
        raise ToolError(f"{source}: {exc}") from exc
    finally:
        try:
            os.unlink(scratch)
        except OSError:
            pass
    return (
        f"Copied {source} -> {target} ({_human_size(info.size)}), by way of "
        "this machine: FTP has no server-side copy, so the bytes made a round "
        "trip. An SFTP connection would have done it on the server."
    )


def symlink_remote(access: AppAccess, args: dict) -> str:
    """Point a name at another path - often the whole of a fix."""
    profile = access.profile(str(args.get("profile", "")))
    access.guard(profile, "Creating symlinks", "allow_write")
    link_path = str(args.get("link_path", "")).strip()
    target = str(args.get("target", "")).strip()
    if not link_path or not target:
        raise ToolError("Both link_path (the new name) and target are required.")
    fs = access.remote(profile)
    if not fs.supports(Capability.SYMLINK):
        raise ToolError(
            f"{profile.label} cannot make symlinks: FTP has no such command. "
            "Connect over SFTP for this."
        )
    existing = None
    try:
        existing = fs.stat(link_path)
    except TransferError:
        pass
    if existing is not None:
        if not existing.is_link:
            raise ToolError(
                f"{link_path} already exists and is a "
                f"{'directory' if existing.is_dir else 'file'}, not a link. "
                "Refusing to replace it: move it out of the way first, so "
                "that what is there now is still there if this was a mistake."
            )
        if not args.get("replace"):
            raise ToolError(
                f"{link_path} is already a link to {existing.link_target or '?'}. "
                "Pass replace=true to repoint it."
            )
        try:
            fs.remove(link_path)
        except TransferError as exc:
            raise ToolError(f"{link_path}: {exc}") from exc
    parent = RemoteFS.parent(link_path)
    if parent not in ("", "/"):
        fs.makedirs(parent)
    try:
        fs.symlink(target, link_path)
    except TransferError as exc:
        raise ToolError(f"{link_path}: {exc}") from exc
    note = ""
    try:
        fs.stat(target if target.startswith("/") else RemoteFS.join(parent, target))
    except TransferError:
        note = (
            "  Note: nothing is at the target yet, so the link is dangling - "
            "which is fine if you are about to create it, and a typo if not."
        )
    return f"{link_path} -> {target}.{note}"


def _two_paths(args: dict) -> tuple[str, str]:
    source = str(args.get("source", "")).strip()
    target = str(args.get("target", "")).strip()
    if not source or not target:
        raise ToolError("Both source and target are required.")
    if source.rstrip("/") in ("", "/") or target.rstrip("/") in ("", "/"):
        raise ToolError("Refusing: name real paths, never the root.")
    if target.rstrip("/") == source.rstrip("/"):
        raise ToolError("The source and the target are the same path.")
    return source, target


def _resolve_target(fs: RemoteFS, source: str, target: str) -> str:
    """Into a directory, or over a name: what the target actually means.

    ``/var/www/keep/`` and an existing ``/var/www/keep`` directory both mean
    "inside it", the way every mv and cp behaves. Anything else is the new
    name itself.
    """
    if target.endswith("/"):
        return RemoteFS.join(target, RemoteFS.basename(source.rstrip("/")))
    try:
        info = fs.stat(target)
    except TransferError:
        return target
    if info.is_dir and not info.is_link:
        return RemoteFS.join(target, RemoteFS.basename(source.rstrip("/")))
    return target


def _octal_mode(value: object) -> int:
    """A mode as an octal string or number. Symbolic modes are refused.

    ``"755"`` and ``0o755`` and ``493`` all mean the same permissions, and
    the first two are what a caller writes. A bare decimal ``755`` is not a
    mode anybody means, so a plain string is read as octal - which is how
    chmod itself reads it - and an int is taken as already being one.
    """
    if isinstance(value, bool) or value is None:
        raise ToolError('Say the mode, e.g. "0755" or "644".')
    if isinstance(value, int):
        return value & 0o7777
    text = str(value).strip().lower()
    if not text:
        raise ToolError('Say the mode, e.g. "0755" or "644".')
    if not all(char in "01234567" for char in text.lstrip("o").lstrip("0o")):
        raise ToolError(
            f"{value!r} is not an octal mode. Use digits - \"644\", "
            '"0755", "2775" - rather than symbolic modes like "g+w".'
        )
    try:
        return int(text, 8) & 0o7777
    except ValueError as exc:
        raise ToolError(f"{value!r} is not an octal mode.") from exc


def run_command(access: AppAccess, args: dict) -> str:
    """Run one command in the server's shell and report what it said."""
    profile = access.profile(str(args.get("profile", "")))
    command = str(args.get("command", "")).strip()
    if not command:
        raise ToolError("Say what to run.")
    cwd = str(args.get("cwd", "")).strip()
    if not cwd and profile.kind == ConnectionKind.SFTP:
        # Only SFTP's start directory can be trusted as a shell path: it is
        # the same filesystem the shell sees. An FTP server often chroots, so
        # its start directory need not exist over SSH, and cd-ing there
        # unasked would fail the command for a reason nobody could act on.
        # Borrowed shells start where the account logs in, which is real.
        cwd = profile.remote_dir.strip()
    timeout = _exec_timeout(args.get("timeout"))
    access.guard(profile, "Running commands on a server", "allow_exec")

    # Handed to the running app when there is one, like every other write:
    # the command then runs on the connection that tab already has open, and
    # lands in the command history beside the ones the user typed. An FTP tab
    # has no shell to lend, which comes back as "unavailable" and is done
    # here instead.
    handed = _hand_op(
        profile, "exec", command=command, cwd=cwd, timeout=timeout
    )
    if handed is not None:
        return handed

    fs = access.shell(profile)
    try:
        result = remote_exec.run(fs, command, cwd=cwd, timeout=timeout)
    except Unsupported as exc:
        raise ToolError(str(exc)) from exc
    except TransferError as exc:
        raise ToolError(f"{profile.label}: {exc}") from exc
    return remote_exec.transcript(
        command, result, label=profile.label, cwd=cwd
    )


def _exec_timeout(value: object) -> float:
    """The timeout to use, clamped rather than refused."""
    try:
        wanted = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_EXEC_TIMEOUT
    if wanted <= 0:
        return DEFAULT_EXEC_TIMEOUT
    return min(wanted, MAX_EXEC_TIMEOUT)


def run_query(access: AppAccess, args: dict) -> str:
    from mysql_runner.db.driver import connect_kwargs, describe_error, import_driver
    from mysql_runner.db.resultformat import format_summary, format_table
    from mysql_runner.db.sqlsplit import split_statements

    profile = access.profile(str(args.get("profile", "")))
    via = str(args.get("via", "")).strip()
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
    if via:
        return _query_through_server(access, profile, via, sql, bool(writes), args)
    if profile.kind != ConnectionKind.MYSQL:
        mysql_labels = [
            p.label for p in access.profiles() if p.kind == ConnectionKind.MYSQL
        ]
        raise ToolError(
            f"{profile.label} is a {profile.kind.value} profile, so there is "
            "no database connection to open. Either name a native MySQL "
            "profile, or pass 'via' with an SFTP/FTP profile on the same "
            "server and the query will run through that server's own mysql "
            "client using these credentials - which is the way in when the "
            "database only listens on localhost, as a shared host's does. "
            + (f"MySQL profiles available: {', '.join(mysql_labels)}." if mysql_labels
               else "No MySQL profiles are stored.")
        )
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


def _query_through_server(
    access: AppAccess, profile, via: str, sql: str, writes: bool, args: dict
) -> str:
    """Run SQL with the server's own mysql client, over its shell.

    This is the way into a database that only listens on localhost, which is
    every shared host: the credentials are in the vault - a phpMyAdmin
    profile's username and password *are* MySQL's - and the client is already
    on the machine. Without it there is no way to run a query against such a
    server at all, because the caller does not have the password and must not
    be given it.

    Two grants apply: running anything on that server needs "Run commands on
    the server", because that is what this is, and a statement that changes
    data needs "Run SQL that changes data" as it would anywhere else.

    The command is never echoed back. It carries the password in its
    environment, and a transcript that quoted it would put a live credential
    into the conversation - which is the one thing this server does not do.
    """
    host_profile = access.profile(via)
    fs = access.shell(host_profile)
    access.guard(host_profile, "Running commands on a server", "allow_exec")
    if writes:
        access.guard(profile, "SQL that changes data", "allow_sql_write")
    user = profile.username
    if not user:
        raise ToolError(
            f"{profile.label} has no username saved, so there is nothing to "
            "log in to MySQL with."
        )
    database = str(args.get("database", "")).strip() or profile.database
    host = str(args.get("db_host", "")).strip()
    if not host:
        # A MySQL profile names its host, and from the server that name may
        # well resolve to something internal. A phpMyAdmin profile names a
        # web address, which is not a database host - from the server itself
        # the answer is almost always localhost.
        host = profile.host if profile.kind == ConnectionKind.MYSQL else "localhost"
    quote = remote_exec.quote
    parts = [
        f"MYSQL_PWD={quote(profile.password)}",
        "mysql",
        f"--host={quote(host)}",
        f"--user={quote(user)}",
        "--batch",
        "--table",
    ]
    if profile.kind == ConnectionKind.MYSQL and profile.effective_port:
        parts.append(f"--port={profile.effective_port}")
    if database:
        parts.append(f"--database={quote(database)}")
    parts.append(f"--execute={quote(sql)}")
    try:
        result = remote_exec.run(fs, " ".join(parts), timeout=_exec_timeout(
            args.get("timeout")
        ))
    except Unsupported as exc:
        raise ToolError(str(exc)) from exc
    except TransferError as exc:
        raise ToolError(f"{host_profile.label}: {exc}") from exc
    body = (result.stdout or "").rstrip()
    problem = _mysql_noise(result.stderr)
    if not result.ok and not body:
        raise ToolError(
            f"{profile.label} on {host_profile.label}: "
            + (problem or f"mysql exited {result.exit_status}")
        )
    lines = [f"mysql> {sql.strip()}", body or "(no rows)"]
    if problem:
        lines.append(f"[{problem}]")
    lines.append(
        f"(run by {host_profile.label}'s own mysql client as "
        f"{user}@{host}{'/' + database if database else ''})"
    )
    return "\n".join(lines)


def _mysql_noise(stderr: str) -> str:
    """The client's complaints, minus the one it makes about every password."""
    kept = [
        line.strip()
        for line in (stderr or "").splitlines()
        if line.strip() and "using a password" not in line.casefold()
    ]
    return "; ".join(kept[:3])


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
        "List a directory on an FTP/FTPS/SFTP server: permissions, "
        "owner:group, size, modified time, and where symlinks point. Read "
        "only - nothing here changes the server. Set depth to walk "
        f"subdirectories too (up to {MAX_TREE_DEPTH}), which beats one call "
        "per directory when mapping a host. Defaults to the profile's start "
        "directory.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "path": {"type": "string", "description": "Remote directory (POSIX style)"},
                "depth": {
                    "type": "integer",
                    "description": (
                        "1 (default) lists just this directory; 2 includes "
                        f"its subdirectories, and so on to {MAX_TREE_DEPTH}."
                    ),
                },
                "details": {
                    "type": "boolean",
                    "description": (
                        "Owner and group as names, which costs one extra "
                        "command on connections that have a shell. Default "
                        "true; false is faster and gives numbers."
                    ),
                },
            },
            "required": ["profile"],
        },
    ),
    "stat_remote": (
        stat_remote,
        "What one or more remote paths are: type, size, modified time, "
        "permissions, owner:group, and a symlink's target. Read only. Use it "
        "before writing somewhere, and to tell a missing file from an "
        "unreadable one.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Remote paths to look at",
                },
                "path": {"type": "string", "description": "One remote path"},
            },
            "required": ["profile"],
        },
    ),
    "check_remote_access": (
        check_remote_access,
        "Whether a given user - the web server's account by default - can "
        "read, write and traverse these paths, and if not, what is in the "
        "way. Answers \"why can PHP not write here\" directly: it reads the "
        "mode, owner and group of each path AND of every parent directory, "
        "looks up that account's groups, and works out which permission "
        "triad actually applies. Read only. Needs a connection with a shell "
        "(SFTP, or FTP borrowing SSH).",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Remote paths to check",
                },
                "path": {"type": "string", "description": "One remote path"},
                "user": {
                    "type": "string",
                    "description": (
                        "The account to ask about - www-data, apache, a "
                        "site's own user. Defaults to whichever usual "
                        "web-server account this server has."
                    ),
                },
            },
            "required": ["profile"],
        },
    ),
    "search_remote": (
        search_remote,
        "Search file contents on the server, recursively - grep -rn, or "
        "ripgrep where it is installed. Read only. This is the companion to "
        "read_remote_file: it answers \"where is this string\" without "
        "downloading the tree, and without having to then prove that what "
        "you searched is what is running. Needs a connection with a shell.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "pattern": {"type": "string", "description": "Text to find"},
                "path": {"type": "string", "description": "Directory to search under (default: the profile's start directory)"},
                "regex": {"type": "boolean", "description": "Treat the pattern as a regular expression (default: literal text)"},
                "ignore_case": {"type": "boolean"},
                "include": {"type": "string", "description": "Only files matching this glob, e.g. *.php"},
                "limit": {"type": "integer", "description": f"Maximum matches (default 200, max {remote_exec.GREP_LIMIT})"},
            },
            "required": ["profile", "pattern"],
        },
    ),
    "diff_remote": (
        diff_remote,
        "Compare a local path with a remote one by content digest, file or "
        "whole tree. Read only. The check worth making before overwriting "
        "production - it says which files differ, which exist on only one "
        "side, and which are byte-for-byte identical, without downloading "
        "anything to find out.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "local_path": {"type": "string", "description": "A local file or directory"},
                "remote_path": {"type": "string", "description": "The remote file or directory to compare it with"},
                "ignore_rules": {
                    "type": "boolean",
                    "description": (
                        "Apply .deployignore/.gitignore when comparing "
                        "directories (default true), so build output and "
                        "node_modules do not read as differences."
                    ),
                },
            },
            "required": ["profile", "local_path", "remote_path"],
        },
    ),
    "chmod_remote": (
        chmod_remote,
        "Set permissions on a remote path. Octal only (\"644\", \"0755\", "
        "\"2775\") - symbolic modes like g+w are refused, so what is being "
        "set is never ambiguous. recursive=true walks a tree, and scope "
        "limits that to 'files' or 'dirs', which is how you set 644 on files "
        "and 755 on directories. Changes the server; needs \"Upload files "
        "and create folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "path": {"type": "string"},
                "mode": {"type": "string", "description": 'Octal, e.g. "0755" or "644"'},
                "recursive": {"type": "boolean"},
                "scope": {
                    "type": "string",
                    "enum": ["all", "files", "dirs"],
                    "description": "With recursive: everything, only files, or only directories",
                },
            },
            "required": ["profile", "path", "mode"],
        },
    ),
    "chown_remote": (
        chown_remote,
        "Change a remote path's owner and/or group. Needs a shell, and "
        "usually needs root for an owner change - a normal account can at "
        "most hand a file to a group it is in. Changes the server; needs "
        "\"Upload files and create folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "path": {"type": "string"},
                "owner": {"type": "string", "description": "New owner (leave out to change only the group)"},
                "group": {"type": "string", "description": "New group"},
                "recursive": {"type": "boolean"},
            },
            "required": ["profile", "path"],
        },
    ),
    "move_remote": (
        move_remote,
        "Move or rename on the server, without the bytes leaving it. A "
        "target that is an existing directory, or ends in /, means 'into "
        "that directory'; anything else is the new name. Overwrites a file "
        "already at the target. Consolidating files this way costs nothing, "
        "where downloading and re-uploading them costs twice their size. "
        "Changes the server; needs \"Upload files and create folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "source": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["profile", "source", "target"],
        },
    ),
    "copy_remote": (
        copy_remote,
        "Copy a file or directory on the server, contents and timestamps "
        "kept. A target that is an existing directory, or ends in /, means "
        "'into that directory'. Runs entirely on the server where there is a "
        "shell; on plain FTP a single file goes by way of this machine and "
        "the answer says so. Changes the server; needs \"Upload files and "
        "create folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "source": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["profile", "source", "target"],
        },
    ),
    "symlink_remote": (
        symlink_remote,
        "Point a name at another path on the server. Often the whole of a "
        "fix - /var/www/uploads -> /var/www/private_data/uploads repairs "
        "every broken path at once, with no code changed. SFTP only (FTP has "
        "no such command). Refuses to replace a real file or directory, and "
        "needs replace=true to repoint an existing link. Changes the server; "
        "needs \"Upload files and create folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "link_path": {"type": "string", "description": "The new name"},
                "target": {"type": "string", "description": "What it should point at"},
                "replace": {"type": "boolean", "description": "Repoint an existing symlink at this name"},
            },
            "required": ["profile", "link_path", "target"],
        },
    ),
    "list_undo_history": (
        list_undo_history,
        "The restore points Sitekeeper kept: every file it overwrote or "
        "deleted, newest first, with the id undo_remote_change takes. Read "
        "only. This is the safety net the app's own History window shows - "
        "worth listing before and after a risky write.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Only this connection's restore points"},
                "limit": {"type": "integer", "description": "How many to list (default 30, max 200)"},
            },
            "required": [],
        },
    ),
    "undo_remote_change": (
        undo_remote_change,
        "Put one file back as it was before Sitekeeper changed it, using an "
        "id from list_undo_history. The kept copy is on this machine, so "
        "restoring a remote file uploads those bytes again. Changes the "
        "server; needs \"Upload files and create folders\".",
        {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "Restore point id (see list_undo_history)"},
            },
            "required": ["entry_id"],
        },
    ),
    "read_remote_file": (
        read_remote_file,
        "Read a text file off the server (configs, logs, source). Read only. "
        "For a log, pass tail_lines: it reads the END of the file, so the "
        "last 200 lines of a 200 MB error log cost the same as a small file "
        "and no download is needed. offset reads a window from a byte "
        "position. Refuses binaries; without tail_lines or offset a file "
        "over max_bytes is refused rather than truncated.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "path": {"type": "string", "description": "Remote file path"},
                "tail_lines": {
                    "type": "integer",
                    "description": (
                        "Return only the last N lines. The way to read a log."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "Start reading at this byte position",
                },
                "max_bytes": {"type": "integer", "description": f"How much to bring back (default {DEFAULT_READ_BYTES}, max {MAX_READ_BYTES})"},
            },
            "required": ["profile", "path"],
        },
    ),
    "download_file": (
        download_file,
        "Download one remote file to a local path, overwriting the local "
        "file if there is one. Does not change the server.",
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
        "Upload ONE local file to the server. For several, use upload_files "
        "instead - one call, one queue - rather than calling this repeatedly. "
        "Overwrites what is at the remote path; the previous bytes go to "
        "Sitekeeper's restore points (see list_undo_history). Missing remote "
        "parent directories are created. Nothing else on the server is "
        "touched. Needs \"Upload files and create folders\".",
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
    "upload_files": (
        upload_files,
        "Upload many files in ONE call - prefer this over calling upload_file "
        "repeatedly. 'files' is a list of local paths, or of {local, remote} "
        "objects when a destination differs. Bare paths land in 'remote_dir' "
        "under their own name, or under their path relative to 'base_dir' "
        "when that is given, which mirrors a subtree. Missing parent "
        "directories are created; if any named path is not a file, nothing "
        "is uploaded and it says which. MERGES: it writes the files it "
        "names and never deletes anything on the server, including files not "
        "present locally. Overwrites a file already at a destination - the "
        "previous bytes go to Sitekeeper's restore points (see "
        "list_undo_history). Reports what happened to each file. Use dry_run "
        "first when writing to a live site. Needs \"Upload files and create "
        f"folders\"; {MAX_FOLDER_FILES} files per call.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "files": {
                    "type": "array",
                    "description": (
                        "Local paths, or {local, remote} objects. A 'remote' "
                        "that does not start with / is taken as relative to "
                        "remote_dir."
                    ),
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "local": {"type": "string"},
                                    "remote": {"type": "string"},
                                },
                                "required": ["local"],
                            },
                        ]
                    },
                },
                "remote_dir": {
                    "type": "string",
                    "description": (
                        "Where files without an absolute 'remote' go. "
                        "Required unless every entry names one."
                    ),
                },
                "base_dir": {
                    "type": "string",
                    "description": (
                        "Local root whose structure to keep under remote_dir, "
                        "e.g. the repository root. Without it, bare paths land "
                        "flat in remote_dir."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Report what would happen - which files are new, "
                        "which would be overwritten, which are already "
                        "byte-for-byte identical - and upload nothing."
                    ),
                },
                "skip_identical": {
                    "type": "boolean",
                    "description": (
                        "Send only what actually differs, comparing by "
                        "digest. Leaves identical files untouched, so their "
                        "timestamps on the server do not move."
                    ),
                },
            },
            "required": ["profile", "files"],
        },
    ),
    "upload_folder": (
        upload_folder,
        "Upload a local folder's contents into a remote directory, honouring "
        ".deployignore/.gitignore. MERGES; NEVER DELETES: a file that exists "
        "on the server but not locally is left exactly as it is, so this is "
        "safe to run into a directory holding files you did not send. "
        "Overwrites files with the same path - their previous bytes go to "
        "Sitekeeper's restore points (see list_undo_history). Use dry_run to "
        "see which files are new, which would be overwritten and which are "
        "already identical, before writing to a live site. When you mean "
        "specific files rather than a whole tree, use upload_files. Needs "
        f"\"Upload files and create folders\"; {MAX_FOLDER_FILES} files per "
        "call.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "local_dir": {"type": "string"},
                "remote_dir": {"type": "string"},
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Report what would happen - which files are new, "
                        "which would be overwritten, which are already "
                        "byte-for-byte identical - and upload nothing."
                    ),
                },
                "skip_identical": {
                    "type": "boolean",
                    "description": (
                        "Send only what actually differs, comparing by "
                        "digest. Leaves identical files untouched, so their "
                        "timestamps on the server do not move."
                    ),
                },
            },
            "required": ["profile", "local_dir", "remote_dir"],
        },
    ),
    "make_remote_dir": (
        make_remote_dir,
        "Create a remote directory, parents included. Does nothing to a "
        "directory that already exists, and never touches its contents. "
        "Needs \"Upload files and create folders\".",
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
        "Delete one remote file, or a directory AND EVERYTHING IN IT. This "
        "is the destructive one: a directory goes recursively, and while "
        "Sitekeeper keeps a copy of what it deletes where it can (see "
        "list_undo_history), a large tree may exceed that limit. Refuses the "
        "root. Needs \"Delete files and folders\".",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["profile", "path"],
        },
    ),
    "run_command": (
        run_command,
        "Run one shell command on an FTP/FTPS/SFTP server and get its output "
        "and exit status. Needs \"Run commands on the server\" in Sitekeeper. "
        "FTP and FTPS have no shell of their own, so those connections are "
        "run over SSH on the same host with the same credentials (port 22 "
        "unless the connection says otherwise). One command per call, in its "
        "own shell: use 'cd x && y' or the cwd argument rather than expecting "
        "a directory change to persist.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Profile label (see list_profiles)"},
                "command": {"type": "string", "description": "The command line to run"},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Directory to run it in. Defaults to the profile's "
                        "start directory on SFTP; on FTP/FTPS it starts "
                        "wherever the SSH account logs in, because an FTP "
                        "path is not always a path on the server itself."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        f"Seconds to wait (default {DEFAULT_EXEC_TIMEOUT:.0f}, "
                        f"max {MAX_EXEC_TIMEOUT:.0f})"
                    ),
                },
            },
            "required": ["profile", "command"],
        },
    ),
    "run_query": (
        run_query,
        "Run SQL on a native MySQL profile and get mysql-client-style "
        "output. SELECT/SHOW/DESCRIBE/EXPLAIN always work; statements that "
        "change data need \"Run SQL that changes data\". A phpMyAdmin "
        "profile has no database connection of its own, but its username and "
        "password ARE MySQL's: pass via=<an SFTP/FTP profile on that server> "
        "and the query runs through the server's own mysql client with those "
        "credentials, which is how to reach a database that only listens on "
        "localhost.",
        {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "sql": {"type": "string", "description": "One or more statements, ; separated"},
                "database": {"type": "string", "description": "Schema to use (defaults to the profile's)"},
                "via": {
                    "type": "string",
                    "description": (
                        "An SFTP/FTP profile on the same server. With this, "
                        "the query runs through that server's own mysql "
                        "client using the named profile's credentials - the "
                        "way in when the database only listens on localhost, "
                        "and what makes a phpMyAdmin profile usable here. "
                        "Needs \"Run commands on the server\" as well."
                    ),
                },
                "db_host": {
                    "type": "string",
                    "description": (
                        "With via: the database host as seen from that "
                        "server (default localhost)."
                    ),
                },
            },
            "required": ["profile", "sql"],
        },
    ),
}
