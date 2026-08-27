"""Shared interface for the remote file-transfer backends.

The file-manager tab is written against RemoteFS only, so FTP, FTPS and SFTP
are interchangeable behind it. Remote paths are always POSIX-style ("/" as the
separator) regardless of protocol.

Protocols differ in what they can do beyond copying bytes: SFTP-over-SSH can
run commands, change permissions and read symlinks, while plain FTP usually
cannot. Rather than let those differences leak out as errors at the point of
use, every backend advertises a set of :class:`Capability` values and callers
ask before offering the feature.
"""

from __future__ import annotations

import os
import posixpath
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

#: progress(transferred_bytes, total_bytes) - total may be 0 when unknown.
ProgressCallback = Callable[[int, int], None]

#: Chunk size used for streaming reads and hashing.
CHUNK = 64 * 1024


class TransferError(Exception):
    """Raised for any backend failure worth showing the user."""


class Unsupported(TransferError):
    """Raised when a backend cannot do what was asked of it.

    Distinct from TransferError so callers can tell "this protocol does not
    have that" from "the server refused".
    """


class Capability(str, Enum):
    """Optional abilities a backend may or may not have."""

    EXEC = "exec"                      # Run shell commands on the server.
    CHMOD = "chmod"                    # Change permission bits.
    SYMLINK = "symlink"                # Read and create symbolic links.
    SET_MTIME = "set_mtime"            # Set a file's modification time.
    ATOMIC_REPLACE = "atomic_replace"  # rename() can overwrite an existing file.


@dataclass(frozen=True)
class RemoteEntry:
    """One directory entry on the remote side."""

    name: str
    is_dir: bool
    size: int = 0
    modified: float | None = None  # Epoch seconds, when the server reports it.
    is_link: bool = False
    #: POSIX permission bits (0o755 and friends), when the server reports them.
    mode: int | None = None
    #: Where a symlink points, when it is cheap to find out.
    link_target: str = ""


@dataclass(frozen=True)
class RemoteStat:
    """What a single stat() call told us about one path."""

    path: str
    is_dir: bool
    size: int = 0
    modified: float | None = None
    mode: int | None = None
    is_link: bool = False


@dataclass(frozen=True)
class ExecResult:
    """The outcome of one remote command."""

    command: str
    exit_status: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def require(self) -> "ExecResult":
        """Return self, or raise with the server's own error text."""
        if self.ok:
            return self
        detail = (self.stderr or self.stdout).strip().splitlines()
        message = detail[0] if detail else f"exit status {self.exit_status}"
        raise TransferError(message)


class RemoteStream(ABC):
    """A running remote command whose output is read as it appears."""

    @abstractmethod
    def read_text(self, timeout: float = 0.5) -> str:
        """Return whatever output has arrived, or "" if none has."""

    @abstractmethod
    def active(self) -> bool:
        """Whether the command is still running."""

    @abstractmethod
    def close(self) -> None:
        """Stop the command and release the channel."""


class ShellChannel(RemoteStream):
    """An interactive shell: like a stream, but you can type into it."""

    @abstractmethod
    def send(self, data: str) -> None:
        """Write to the shell's standard input."""

    def resize(self, width: int, height: int) -> None:
        """Tell the far end the terminal changed size (optional)."""


class RemoteFS(ABC):
    """A connected remote filesystem."""

    @abstractmethod
    def connect(self) -> str:
        """Open the connection. Returns a short banner for the status line."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection, ignoring errors."""

    @abstractmethod
    def home(self) -> str:
        """The directory to show when the session opens."""

    @abstractmethod
    def listdir(self, path: str) -> list[RemoteEntry]:
        """List a directory, excluding the "." and ".." entries."""

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Create a directory."""

    @abstractmethod
    def remove(self, path: str) -> None:
        """Delete a file."""

    @abstractmethod
    def rmdir(self, path: str) -> None:
        """Delete an empty directory."""

    @abstractmethod
    def rename(self, source: str, target: str) -> None:
        """Rename or move an entry."""

    @abstractmethod
    def download(
        self, remote: str, local: str, progress: ProgressCallback | None = None
    ) -> None:
        """Copy a remote file to a local path."""

    @abstractmethod
    def upload(
        self, local: str, remote: str, progress: ProgressCallback | None = None
    ) -> None:
        """Copy a local file to a remote path."""

    # ----- capabilities ---------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        """What this backend can do beyond the abstract methods above."""
        return frozenset()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    def _require_capability(self, capability: Capability) -> None:
        if not self.supports(capability):
            wording = capability.value.replace("_", " ")
            raise Unsupported(f"This connection cannot {wording}.")

    def alive(self) -> bool:
        """Best-effort: whether the session still answers at all.

        Idle connections get dropped by servers and firewalls without a word;
        this is how callers tell "the server refused that operation" from
        "the connection is gone", which is the case worth reconnecting for.
        The default pays one round trip; backends override it with something
        cheaper where the protocol has one.
        """
        try:
            self.home()
        except Exception:
            return False
        return True

    # ----- optional operations -------------------------------------------
    # Default implementations either emulate the operation with what every
    # backend has, or refuse politely. Backends override what they can do.
    def stat(self, path: str) -> RemoteStat:
        """Stat one path. The default derives it from the parent listing."""
        name = self.basename(path)
        parent = self.parent(path)
        for entry in self.listdir(parent):
            if entry.name == name:
                return RemoteStat(
                    path=path,
                    is_dir=entry.is_dir,
                    size=entry.size,
                    modified=entry.modified,
                    mode=entry.mode,
                    is_link=entry.is_link,
                )
        raise TransferError(f"{path} does not exist.")

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except TransferError:
            return False
        return True

    def chmod(self, path: str, mode: int) -> None:
        self._require_capability(Capability.CHMOD)
        raise Unsupported("chmod is not implemented for this backend.")

    def readlink(self, path: str) -> str:
        self._require_capability(Capability.SYMLINK)
        raise Unsupported("Reading symlinks is not implemented for this backend.")

    def symlink(self, target: str, link_path: str) -> None:
        self._require_capability(Capability.SYMLINK)
        raise Unsupported("Creating symlinks is not implemented for this backend.")

    def set_mtime(self, path: str, mtime: float) -> None:
        self._require_capability(Capability.SET_MTIME)
        raise Unsupported("Setting timestamps is not implemented for this backend.")

    def exec_command(self, command: str, *, timeout: float = 60.0) -> ExecResult:
        self._require_capability(Capability.EXEC)
        raise Unsupported("Running commands is not implemented for this backend.")

    def exec_stream(self, command: str) -> "RemoteStream":
        """Start a command and read its output as it arrives.

        Needed by anything open-ended - ``tail -f`` above all - where waiting
        for the process to exit would mean waiting forever.
        """
        self._require_capability(Capability.EXEC)
        raise Unsupported("Streaming commands is not implemented for this backend.")

    def open_shell(self, *, width: int = 120, height: int = 32) -> "ShellChannel":
        """Start an interactive login shell on the server."""
        self._require_capability(Capability.EXEC)
        raise Unsupported("Interactive shells are not implemented for this backend.")

    def replace(self, source: str, target: str) -> None:
        """Rename ``source`` onto ``target``, replacing it if it exists.

        SFTP servers with the OpenSSH POSIX-rename extension do this in one
        atomic step. Everywhere else the target has to be unlinked first, which
        leaves a sliver of time with no file in place - still far better than
        writing over a live file byte by byte.
        """
        if self.supports(Capability.ATOMIC_REPLACE):
            self.rename(source, target)
            return
        try:
            self.remove(target)
        except TransferError:
            pass  # Absent, or a directory - let the rename report the truth.
        self.rename(source, target)

    def stream_download(
        self,
        remote: str,
        sink: Callable[[bytes], None],
        progress: ProgressCallback | None = None,
    ) -> int:
        """Feed a remote file to ``sink`` in chunks. Returns the byte count.

        Used for hashing and previewing without keeping a copy. The default
        goes through a temporary file so any backend gets the behaviour; both
        shipped backends override it with a direct stream.
        """
        handle = tempfile.NamedTemporaryFile(prefix="mrstream-", delete=False)
        temp = handle.name
        handle.close()
        try:
            self.download(remote, temp, progress)
            total = 0
            with open(temp, "rb") as source:
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    sink(chunk)
            return total
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass

    def walk(self, path: str, *, follow_links: bool = False):
        """Yield (directory, entries) pairs, breadth-first-ish, like os.walk.

        Symlinked directories are skipped by default: a link pointing at its
        own ancestor would otherwise walk forever.
        """
        stack = [path]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            entries = self.listdir(current)
            yield current, entries
            for entry in entries:
                if not entry.is_dir:
                    continue
                if entry.is_link and not follow_links:
                    continue
                stack.append(self.join(current, entry.name))

    def makedirs(self, path: str) -> None:
        """Create a directory and any missing parents, ignoring existing ones."""
        parts = [p for p in posixpath.normpath(path).split("/") if p]
        absolute = path.startswith("/")
        current = "/" if absolute else ""
        for part in parts:
            current = self.join(current, part) if current else part
            try:
                self.mkdir(current)
            except TransferError:
                pass  # Already there, or the next call will report the truth.

    # ----- path helpers (POSIX semantics for every backend) --------------
    @staticmethod
    def join(base: str, name: str) -> str:
        return posixpath.normpath(posixpath.join(base or "/", name))

    @staticmethod
    def parent(path: str) -> str:
        parent = posixpath.dirname(posixpath.normpath(path or "/"))
        return parent or "/"

    @staticmethod
    def basename(path: str) -> str:
        return posixpath.basename(posixpath.normpath(path or "/"))


def temp_name(remote: str, token: str) -> str:
    """The scratch path an atomic upload writes to before its final rename."""
    return f"{remote}.mrtmp-{token}"


def relative_posix(base: str, path: str) -> str:
    """``path`` expressed relative to ``base``, POSIX-style, "" when equal."""
    base_norm = posixpath.normpath(base or "/")
    path_norm = posixpath.normpath(path or "/")
    if path_norm == base_norm:
        return ""
    prefix = base_norm.rstrip("/") + "/"
    if path_norm.startswith(prefix):
        return path_norm[len(prefix):]
    return path_norm.lstrip("/")


def local_relative(base: str, path: str) -> str:
    """Local path relative to ``base``, always with forward slashes."""
    try:
        rel = os.path.relpath(path, base)
    except ValueError:  # different drives on Windows
        return os.path.basename(path)
    return rel.replace(os.sep, "/")


def sort_entries(entries: Iterable[RemoteEntry]) -> list[RemoteEntry]:
    """Directories first, then names case-insensitively - the WinSCP order."""
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))
