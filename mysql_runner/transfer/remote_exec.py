"""Server-side tools that need a shell: archives, grep, disk usage, tail.

Everything here runs *on the server*, which is the whole point: extracting an
archive of ten thousand small files over SFTP takes minutes, while asking the
server to do it takes a second. Only SFTP-over-SSH can do any of it, so each
helper is guarded by :data:`Capability.EXEC` and the UI hides what a plain FTP
session cannot offer.

Command lines are built with :func:`quote`, never by interpolating raw user
input, so a path with a space or a quote in it cannot turn into extra commands.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import string
from dataclasses import dataclass, field

from mysql_runner.transfer.base import (
    Capability,
    ExecResult,
    RemoteFS,
    RemoteStream,
    TransferError,
    Unsupported,
)
from mysql_runner.transfer.hashing import FileInfo, TreeSnapshot

#: Attribute used to cache "does this server have that tool" answers per session.
_TOOL_CACHE = "_mr_tool_cache"

#: Digest tools, in the order they are tried. Each entry maps an algorithm to
#: the command that prints "<hex>  <path>".
_DIGEST_TOOLS = {
    "sha256": ("sha256sum", "shasum -a 256", "openssl dgst -r -sha256"),
    "sha1": ("sha1sum", "shasum -a 1", "openssl dgst -r -sha1"),
    "md5": ("md5sum", "md5 -r", "openssl dgst -r -md5"),
}

#: Upper bound on grep hits brought back, so a match-everything pattern cannot
#: flood the UI.
GREP_LIMIT = 500


def quote(value: str) -> str:
    """POSIX-quote one argument for a remote command line."""
    return shlex.quote(value)


def require_exec(fs: RemoteFS) -> None:
    """Raise a clear error when the session has no shell."""
    if not fs.supports(Capability.EXEC):
        raise Unsupported(
            "This is a file-transfer-only connection, so server-side commands "
            "are not available. Use an SFTP connection for those."
        )


def run(fs: RemoteFS, command: str, *, cwd: str = "", timeout: float = 60.0) -> ExecResult:
    """Run one command, optionally after changing directory."""
    require_exec(fs)
    full = f"cd -- {quote(cwd)} && {command}" if cwd else command
    return fs.exec_command(full, timeout=timeout)


def has_tool(fs: RemoteFS, name: str) -> bool:
    """Whether a command exists on the server (cached per connection)."""
    cache = getattr(fs, _TOOL_CACHE, None)
    if cache is None:
        cache = {}
        setattr(fs, _TOOL_CACHE, cache)
    if name in cache:
        return cache[name]
    try:
        result = run(fs, f"command -v {quote(name)} >/dev/null 2>&1", timeout=15)
        present = result.ok
    except TransferError:
        present = False
    cache[name] = present
    return present


def tar_extra_flags(fs: RemoteFS) -> str:
    """Extra tar flags this server needs, cached per connection.

    GNU tar reads any argument containing a colon as ``host:path`` and tries to
    reach it over rsh, so an archive under a directory with a colon in its name
    fails in a baffling way. ``--force-local`` turns that off - but only GNU tar
    has the flag, so it is only used where it exists.
    """
    cache = getattr(fs, _TOOL_CACHE, None)
    if cache is None:
        cache = {}
        setattr(fs, _TOOL_CACHE, cache)
    if "tar:gnu" in cache:
        return "--force-local " if cache["tar:gnu"] else ""
    gnu = False
    try:
        result = run(fs, "tar --version 2>/dev/null | head -n 1", timeout=20)
        gnu = "GNU tar" in result.stdout
    except TransferError:
        gnu = False
    cache["tar:gnu"] = gnu
    return "--force-local " if gnu else ""


def uname(fs: RemoteFS) -> str:
    """The server's ``uname -sr``, for the terminal banner."""
    try:
        return run(fs, "uname -sr", timeout=15).stdout.strip()
    except TransferError:
        return ""


# ----- digests ------------------------------------------------------------
def digest_command(fs: RemoteFS, algorithm: str) -> str:
    """The digest command available on this server, or "" if none is."""
    for candidate in _DIGEST_TOOLS.get(algorithm, ()):  # noqa: SIM110 - readability
        tool = candidate.split(" ", 1)[0]
        if has_tool(fs, tool):
            return candidate
    return ""


def remote_digest(fs: RemoteFS, path: str, *, algorithm: str = "sha256") -> str:
    """Hash one remote file server-side. Returns "" when no tool can do it."""
    command = digest_command(fs, algorithm)
    if not command:
        return ""
    try:
        result = run(fs, f"{command} -- {quote(path)}", timeout=300)
    except TransferError:
        return ""
    if not result.ok:
        return ""
    return _first_hex(result.stdout)


def digest_tree(
    fs: RemoteFS,
    root: str,
    *,
    algorithm: str = "sha256",
    max_files: int = 20_000,
) -> TreeSnapshot | None:
    """Hash a whole remote tree with one command.

    Returns None when the server has no digest tool, which tells the caller to
    fall back to hashing over the transfer channel.
    """
    command = digest_command(fs, algorithm)
    if not command:
        return None
    listing = (
        f"find . -type f -print0 2>/dev/null | xargs -0 -r {command} 2>/dev/null"
    )
    try:
        result = run(fs, listing, cwd=root, timeout=900)
    except TransferError:
        return None
    if not result.stdout.strip() and not result.ok:
        return None

    snapshot = TreeSnapshot(root=root)
    for line in result.stdout.splitlines():
        parsed = _parse_digest_line(line)
        if parsed is None:
            continue
        digest, rel = parsed
        if len(snapshot.files) >= max_files:
            snapshot.truncated = True
            break
        snapshot.files[rel] = FileInfo(rel=rel, digest=digest)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent:
            snapshot.dirs.add(parent)
    _fill_sizes(fs, root, snapshot)
    return snapshot


def _parse_digest_line(line: str) -> tuple[str, str] | None:
    """Parse one "<hex>  ./some/path" line into (digest, relative path)."""
    parts = line.strip().split(None, 1)
    if len(parts) != 2:
        return None
    digest, rel = parts
    if len(digest) < 8 or digest.strip(string.hexdigits):
        return None
    # "openssl dgst -r" prints "<hex> *path"; the sum tools print "<hex>  path".
    rel = rel.strip().lstrip("*")
    if rel.startswith("./"):
        rel = rel[2:]
    return digest.lower(), rel


def _fill_sizes(fs: RemoteFS, root: str, snapshot: TreeSnapshot) -> None:
    """Add sizes and timestamps to a digest-only snapshot, if find can."""
    try:
        result = run(
            fs,
            "find . -type f -printf '%s\\t%T@\\t%p\\n' 2>/dev/null",
            cwd=root,
            timeout=300,
        )
    except TransferError:
        return
    if not result.ok:
        return  # BSD find has no -printf; sizes stay unknown, digests still work.
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        size_text, mtime_text, rel = parts
        if rel.startswith("./"):
            rel = rel[2:]
        info = snapshot.files.get(rel)
        if info is None:
            continue
        snapshot.files[rel] = FileInfo(
            rel=rel,
            size=_as_int(size_text),
            modified=_as_float(mtime_text),
            digest=info.digest,
        )


def _first_hex(text: str) -> str:
    for line in text.splitlines():
        match = re.search(r"\b([0-9a-fA-F]{16,})\b", line)
        if match is not None:
            return match.group(1).lower()
    return ""


# ----- disk usage ---------------------------------------------------------
@dataclass(frozen=True)
class DuEntry:
    """One row of a disk-usage report."""

    name: str
    path: str
    size: int
    is_dir: bool = True


@dataclass
class DiskUsage:
    """What lives under one directory, largest first."""

    root: str
    entries: list[DuEntry] = field(default_factory=list)
    total: int = 0

    def share(self, entry: DuEntry) -> float:
        """Fraction of the total this entry accounts for (0.0 - 1.0)."""
        return entry.size / self.total if self.total else 0.0


def disk_usage(fs: RemoteFS, path: str, *, timeout: float = 300.0) -> DiskUsage:
    """One level of ``du``, sorted biggest first - the ncdu view."""
    require_exec(fs)
    flavours = (
        f"du -k --max-depth=1 -- {quote(path)}",  # GNU
        f"du -k -d 1 -- {quote(path)}",           # BSD / busybox
    )
    last: ExecResult | None = None
    for command in flavours:
        result = run(fs, f"{command} 2>/dev/null", timeout=timeout)
        last = result
        if result.stdout.strip():
            return _parse_du(path, result.stdout)
    if last is not None and not last.ok:
        last.require()
    return DiskUsage(root=path)


def _parse_du(path: str, output: str) -> DiskUsage:
    """Turn du output into a report, telling the total row from the children.

    du prints the directory it was asked about last, but not necessarily under
    the name it was given: a trailing slash, a resolved symlink or a relative
    argument all come back changed. So the total row is recognised as the one
    every other row sits inside, and only failing that by name.
    """
    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        rows.append((_as_int(parts[0]) * 1024, parts[1].strip()))  # du -k: kibibytes
    if not rows:
        return DiskUsage(root=path)

    total_index = _total_row(rows, path)
    report = DiskUsage(root=path, total=rows[total_index][0])
    for index, (size, entry_path) in enumerate(rows):
        if index == total_index:
            continue
        name = entry_path.rstrip("/").rsplit("/", 1)[-1]
        report.entries.append(DuEntry(name=name, path=entry_path, size=size))
    report.entries.sort(key=lambda e: e.size, reverse=True)
    if not report.total:
        report.total = sum(entry.size for entry in report.entries)
    return report


def _total_row(rows: list[tuple[int, str]], path: str) -> int:
    """Which row is the directory itself rather than one of its children."""
    wanted = path.rstrip("/")
    for index, (_size, entry_path) in enumerate(rows):
        if entry_path.rstrip("/") == wanted:
            return index
    for index, (_size, entry_path) in enumerate(rows):
        prefix = entry_path.rstrip("/") + "/"
        others = [other for position, (_s, other) in enumerate(rows) if position != index]
        if others and all(other.startswith(prefix) for other in others):
            return index
    return len(rows) - 1  # du prints the total last


# ----- search -------------------------------------------------------------
@dataclass(frozen=True)
class GrepHit:
    """One matching line."""

    path: str
    line: int
    text: str


@dataclass
class GrepResult:
    """The outcome of a server-side search."""

    hits: list[GrepHit] = field(default_factory=list)
    tool: str = ""
    truncated: bool = False
    error: str = ""


def grep(
    fs: RemoteFS,
    root: str,
    pattern: str,
    *,
    fixed: bool = True,
    ignore_case: bool = False,
    include: str = "",
    limit: int = GREP_LIMIT,
    timeout: float = 300.0,
) -> GrepResult:
    """Search file contents on the server, ripgrep first, grep second."""
    require_exec(fs)
    if not pattern:
        return GrepResult(error="Nothing to search for.")
    use_rg = has_tool(fs, "rg")
    command = (
        _rg_command(pattern, root, fixed, ignore_case, include, limit)
        if use_rg
        else _grep_command(pattern, root, fixed, ignore_case, include, limit)
    )
    result = run(fs, command, timeout=timeout)
    hits = [hit for hit in map(_parse_grep_line, result.stdout.splitlines()) if hit]
    outcome = GrepResult(hits=hits[:limit], tool="rg" if use_rg else "grep")
    outcome.truncated = len(hits) >= limit
    if not hits and result.exit_status not in (0, 1, 141) and result.stderr.strip():
        outcome.error = result.stderr.strip().splitlines()[0]
    return outcome


def _rg_command(
    pattern: str, root: str, fixed: bool, ignore_case: bool, include: str, limit: int
) -> str:
    parts = ["rg", "--line-number", "--no-heading", "--color", "never", "--no-messages"]
    if fixed:
        parts.append("--fixed-strings")
    if ignore_case:
        parts.append("--ignore-case")
    if include:
        parts.extend(["--glob", quote(include)])
    parts.extend(["-e", quote(pattern), "--", quote(root)])
    return " ".join(parts) + f" | head -n {int(limit)}"


def _grep_command(
    pattern: str, root: str, fixed: bool, ignore_case: bool, include: str, limit: int
) -> str:
    parts = ["grep", "-rnI", "--binary-files=without-match"]
    parts.append("-F" if fixed else "-E")
    if ignore_case:
        parts.append("-i")
    if include:
        parts.append(f"--include={quote(include)}")
    parts.extend(["-e", quote(pattern), "--", quote(root)])
    return " ".join(parts) + f" 2>/dev/null | head -n {int(limit)}"


def _parse_grep_line(line: str) -> GrepHit | None:
    """Parse "path:line:text", tolerating colons inside the path."""
    start = 0
    while True:
        colon = line.find(":", start)
        if colon == -1:
            return None
        rest = line[colon + 1:]
        second = rest.find(":")
        if second == -1:
            return None
        number = rest[:second]
        if number.isdigit():
            return GrepHit(
                path=line[:colon],
                line=int(number),
                text=rest[second + 1:].rstrip(),
            )
        start = colon + 1


# ----- archives -----------------------------------------------------------
#: Archive kind identifiers, used by both the packer and the unpacker.
TAR_GZ = "tar.gz"
TAR_BZ2 = "tar.bz2"
TAR = "tar"
ZIP = "zip"

#: Archive kinds offered in the UI, mapped to the suffix each one writes.
ARCHIVE_KINDS = {
    TAR_GZ: ".tar.gz",
    TAR_BZ2: ".tar.bz2",
    TAR: ".tar",
    ZIP: ".zip",
}

#: tar's flags for creating and for extracting each kind. The leading dash
#: matters: tar only reads a bare "czf" as bundled options when it is the very
#: first argument, and --force-local may come before it.
_TAR_CREATE = {TAR_GZ: "-czf", TAR_BZ2: "-cjf", TAR: "-cf"}
_TAR_EXTRACT = {TAR_GZ: "-xzf", TAR_BZ2: "-xjf", TAR: "-xf"}


def archive_kind_for(name: str) -> str:
    """Which archive kind a filename looks like, "" when unrecognised."""
    lowered = name.lower()
    if lowered.endswith((".tar.gz", ".tgz")):
        return TAR_GZ
    if lowered.endswith((".tar.bz2", ".tbz2")):
        return TAR_BZ2
    if lowered.endswith(".tar"):
        return TAR
    if lowered.endswith(".zip"):
        return ZIP
    return ""


def make_archive(
    fs: RemoteFS,
    directory: str,
    names: list[str],
    archive: str,
    *,
    kind: str = TAR_GZ,
    timeout: float = 1800.0,
) -> ExecResult:
    """Pack ``names`` (relative to ``directory``) into ``archive`` on the server."""
    require_exec(fs)
    if not names:
        raise TransferError("Nothing selected to archive.")
    quoted = " ".join(quote(name) for name in names)
    if kind == ZIP:
        if not has_tool(fs, ZIP):
            raise Unsupported(
                "The server has no 'zip' command. Choose a .tar.gz archive instead."
            )
        command = f"zip -r -q -- {quote(archive)} {quoted}"
    else:
        if not has_tool(fs, TAR):
            raise Unsupported("The server has no 'tar' command.")
        extra = tar_extra_flags(fs)
        command = (
            f"tar {extra}{_TAR_CREATE.get(kind, '-czf')} {quote(archive)} -- {quoted}"
        )
    return run(fs, command, cwd=directory, timeout=timeout).require()


def extract_archive(
    fs: RemoteFS,
    archive: str,
    destination: str,
    *,
    timeout: float = 1800.0,
) -> ExecResult:
    """Unpack an archive that is already on the server."""
    require_exec(fs)
    kind = archive_kind_for(archive)
    if kind == ZIP:
        if not has_tool(fs, "unzip"):
            raise Unsupported("The server has no 'unzip' command.")
        command = f"unzip -o -q -- {quote(archive)} -d {quote(destination)}"
    elif kind:
        if not has_tool(fs, TAR):
            raise Unsupported("The server has no 'tar' command.")
        extra = tar_extra_flags(fs)
        command = (
            f"tar {extra}{_TAR_EXTRACT[kind]} {quote(archive)} -C {quote(destination)}"
        )
    else:
        raise Unsupported(
            "Only .tar, .tar.gz, .tar.bz2 and .zip archives can be unpacked."
        )
    return run(fs, f"mkdir -p -- {quote(destination)} && {command}", timeout=timeout).require()


# ----- permissions --------------------------------------------------------
def chmod_tree(
    fs: RemoteFS,
    path: str,
    mode: int,
    *,
    scope: str = "all",
    timeout: float = 600.0,
) -> ExecResult:
    """Recursive chmod. ``scope`` is "all", "files" or "dirs"."""
    require_exec(fs)
    octal = format(mode & 0o7777, "o")
    if scope == "files":
        command = f"find {quote(path)} -type f -exec chmod {octal} {{}} +"
    elif scope == "dirs":
        command = f"find {quote(path)} -type d -exec chmod {octal} {{}} +"
    else:
        command = f"chmod -R {octal} -- {quote(path)}"
    return run(fs, command, timeout=timeout).require()


def stat_details(fs: RemoteFS, path: str) -> str:
    """A human-readable ``ls -ld`` line for the properties dialog."""
    try:
        return run(fs, f"ls -ld -- {quote(path)}", timeout=30).stdout.strip()
    except TransferError:
        return ""


# ----- streaming ----------------------------------------------------------
def tail(fs: RemoteFS, path: str, *, lines: int = 200, follow: bool = True) -> RemoteStream:
    """Start streaming the end of a log file."""
    require_exec(fs)
    flag = "-f" if follow else ""
    command = f"tail -n {int(lines)} {flag} -- {quote(path)}".replace("  ", " ")
    return fs.exec_stream(command)


def list_logs(fs: RemoteFS, directory: str, *, limit: int = 40) -> list[str]:
    """Log-looking files under a directory, newest first, for the log picker."""
    require_exec(fs)
    command = (
        "find . -maxdepth 2 -type f \\( -name '*.log' -o -name '*log' -o "
        "-name '*.err' \\) -printf '%T@\\t%p\\n' 2>/dev/null | sort -nr | "
        f"head -n {int(limit)}"
    )
    result = run(fs, command, cwd=directory, timeout=60)
    paths: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            paths.append(parts[1].strip())
    return paths


# ----- misc ---------------------------------------------------------------
def _as_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _as_float(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def content_digest(data: bytes, *, algorithm: str = "sha256") -> str:
    """Digest of a bytes blob - used for small in-memory comparisons."""
    return hashlib.new(algorithm, data).hexdigest()
