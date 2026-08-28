"""SFTP backend built on Paramiko.

Host keys follow trust-on-first-use: a key seen for the first time is recorded
in %APPDATA%\\Sitekeeper\\known_hosts, and a later mismatch for that host
aborts the connection rather than connecting anyway. Delete the offending line
from that file if a server is legitimately rekeyed.

SFTP rides on SSH, so this backend can do everything the protocol allows and
then some: permissions, symlinks, timestamps, atomic renames, and running
commands on the server for the archive, search and disk-usage tools.
"""

from __future__ import annotations

import os
import stat
import time

from mysql_runner.paths import known_hosts_path
from mysql_runner.transfer.base import (
    CHUNK,
    Capability,
    ExecResult,
    ProgressCallback,
    RemoteEntry,
    RemoteFS,
    RemoteStat,
    RemoteStream,
    ShellChannel,
    TransferError,
)

#: Timeout for the TCP/handshake phase, in seconds.
TIMEOUT = 20

#: How long to wait for one read from a streaming command before giving the
#: caller a turn (it may want to stop, or update a UI).
STREAM_POLL = 0.2

#: Seconds allowed for the "can this account run commands at all" probe.
PROBE_TIMEOUT = 6.0

#: SSH channel window for the SFTP session. Paramiko's default (2 MB) caps
#: how much data may be in flight unacknowledged, which throttles transfers
#: badly on anything with real latency - the single biggest reason uploads
#: here used to lose to WinSCP on the same link.
WINDOW_SIZE = 16 * 1024 * 1024

#: Used whenever an operation arrives with no live session.
NOT_CONNECTED = "Not connected."


class SFTPUnavailable(RuntimeError):
    """Raised when the SSH library is missing from this build."""


def import_paramiko():
    """Import Paramiko lazily so builds without it still start."""
    try:
        import paramiko  # noqa: PLC0415 - deliberately deferred
    except ImportError as exc:  # pragma: no cover - depends on the build
        raise SFTPUnavailable(
            "The SSH library (Paramiko) is not available in this build, so "
            "SFTP connections cannot be opened."
        ) from exc
    return paramiko


def driver_available() -> bool:
    """Whether SFTP connections are possible in this build."""
    try:
        import_paramiko()
    except SFTPUnavailable:
        return False
    return True


#: What SFTP-over-SSH can do beyond plain file transfer.
_CAPABILITIES = frozenset(
    {
        Capability.EXEC,
        Capability.CHMOD,
        Capability.SYMLINK,
        Capability.SET_MTIME,
    }
)


class _ChannelStream(RemoteStream):
    """Output of a running remote command, read as it arrives."""

    def __init__(self, channel, command: str) -> None:
        self._channel = channel
        self._command = command
        channel.settimeout(STREAM_POLL)

    def read_text(self, timeout: float = STREAM_POLL) -> str:
        chunks: list[bytes] = []
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._channel.recv_ready():
                chunks.append(self._channel.recv(CHUNK))
            elif self._channel.recv_stderr_ready():
                chunks.append(self._channel.recv_stderr(CHUNK))
            elif chunks or time.monotonic() >= deadline:
                break
            else:
                time.sleep(0.02)
        if not chunks:
            return ""
        return b"".join(chunks).decode("utf-8", errors="replace")

    def active(self) -> bool:
        channel = self._channel
        if channel is None:
            return False
        if channel.recv_ready() or channel.recv_stderr_ready():
            return True
        return not channel.exit_status_ready()

    def close(self) -> None:
        channel, self._channel = self._channel, None
        if channel is None:
            return
        try:
            channel.close()
        except Exception:
            pass


class _ShellStream(_ChannelStream, ShellChannel):
    """An interactive shell channel."""

    def send(self, data: str) -> None:
        channel = self._channel
        if channel is None:
            raise TransferError("The shell has closed.")
        try:
            channel.send(data.encode("utf-8"))
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def resize(self, width: int, height: int) -> None:
        channel = self._channel
        if channel is None:
            return
        try:
            channel.resize_pty(width=max(20, width), height=max(5, height))
        except Exception:
            pass


class SFTPFileSystem(RemoteFS):
    """SFTP-over-SSH remote filesystem."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        private_key_path: str = "",
    ) -> None:
        self._host = host
        self._port = port or 22
        self._username = username
        self._password = password
        self._key_path = private_key_path
        self._client = None
        self._sftp = None
        self._posix_rename = True  # Until a server says otherwise.
        self._caps = _CAPABILITIES  # Narrowed by the probe in connect().

    # ----- lifecycle ------------------------------------------------------
    def connect(self) -> str:
        paramiko = import_paramiko()
        client = paramiko.SSHClient()
        hosts_file = known_hosts_path()
        if hosts_file.exists():
            try:
                client.load_host_keys(str(hosts_file))
            except (OSError, paramiko.SSHException):
                # A corrupt known_hosts must not lock the user out; a new key
                # will simply be recorded again below.
                pass
        # Record unknown keys, but reject a *changed* key for a known host.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict[str, object] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._username,
            "timeout": TIMEOUT,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if self._key_path:
            kwargs["key_filename"] = self._key_path
            # A passphrase-protected key reuses the stored password field.
            if self._password:
                kwargs["passphrase"] = self._password
        else:
            kwargs["password"] = self._password

        try:
            client.connect(**kwargs)  # type: ignore[arg-type]
        except paramiko.BadHostKeyException as exc:
            raise TransferError(
                f"The host key for {self._host} does not match the one stored "
                f"in {hosts_file.name}. If this server was legitimately "
                "rekeyed, remove its line from that file and reconnect."
            ) from exc
        except paramiko.AuthenticationException as exc:
            raise TransferError("Authentication failed.") from exc
        except (paramiko.SSHException, OSError) as exc:
            raise TransferError(_describe(exc)) from exc

        try:
            client.save_host_keys(str(hosts_file))
        except (OSError, paramiko.SSHException):
            # Persisting the key is a convenience; carry on without it.
            pass

        transport = client.get_transport()
        try:
            if transport is None:
                raise TransferError("The SSH transport closed during login.")
            self._sftp = paramiko.SFTPClient.from_transport(
                transport, window_size=WINDOW_SIZE
            )
            if self._sftp is None:
                raise TransferError("The server refused to open an SFTP session.")
        except TransferError:
            client.close()
            raise
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise TransferError(_describe(exc)) from exc

        self._client = client
        self._caps = _CAPABILITIES
        if not self._shell_works():
            self._caps = _CAPABILITIES - {Capability.EXEC}
        banner = transport.remote_version or ""
        return f"SFTP connected to {self._host}:{self._port}. {banner}".strip()

    def _shell_works(self) -> bool:
        """Whether this account may actually run commands.

        Plenty of hosting accounts are SFTP-only: the file transfer works but
        every exec request is refused. One cheap probe at connect time is what
        lets the UI hide the server-side tools instead of offering buttons that
        fail when pressed.
        """
        try:
            return self.exec_command("printf mrok", timeout=PROBE_TIMEOUT).ok
        except TransferError:
            return False
        except Exception:
            return False

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _require(self):
        if self._sftp is None:
            raise TransferError(NOT_CONNECTED)
        return self._sftp

    def _require_client(self):
        if self._client is None:
            raise TransferError(NOT_CONNECTED)
        return self._client

    def alive(self) -> bool:
        """Whether the SSH session still answers.

        The transport flag flips the moment Paramiko's reader thread sees the
        connection die, so the common case costs nothing; the ``realpath``
        round trip catches a session that is up but no longer serving SFTP.
        """
        client, sftp = self._client, self._sftp
        if client is None or sftp is None:
            return False
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return False
        try:
            sftp.normalize(".")
        except Exception:
            return False
        return True

    # ----- capabilities ---------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return self._caps

    # ----- navigation -----------------------------------------------------
    def home(self) -> str:
        sftp = self._require()
        try:
            return sftp.normalize(".")
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def listdir(self, path: str) -> list[RemoteEntry]:
        sftp = self._require()
        try:
            attrs = sftp.listdir_attr(path or "/")
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        entries: list[RemoteEntry] = []
        for attr in attrs:
            if attr.filename in (".", ".."):
                continue
            entries.append(self._entry_from_attr(path, attr))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def _entry_from_attr(self, path: str, attr) -> RemoteEntry:
        mode = attr.st_mode or 0
        is_link = stat.S_ISLNK(mode)
        is_dir = stat.S_ISDIR(mode)
        target = ""
        if is_link:
            # Resolve symlinks so a link to a directory is navigable, and show
            # where it points - a release symlink is the whole story of a deploy.
            is_dir = self._link_is_dir(path, attr.filename)
            target = self._link_target(path, attr.filename)
        return RemoteEntry(
            name=attr.filename,
            is_dir=is_dir,
            size=int(attr.st_size or 0),
            modified=float(attr.st_mtime) if attr.st_mtime else None,
            is_link=is_link,
            mode=stat.S_IMODE(mode) if mode else None,
            link_target=target,
        )

    def _link_is_dir(self, path: str, name: str) -> bool:
        try:
            target = self._require().stat(self.join(path, name))
        except Exception:
            return False
        return stat.S_ISDIR(target.st_mode or 0)

    def _link_target(self, path: str, name: str) -> str:
        try:
            return self._require().readlink(self.join(path, name)) or ""
        except Exception:
            return ""

    def stat(self, path: str) -> RemoteStat:
        # lstat first, and follow only actual links: one round trip per file
        # instead of two, which the transfer queue pays for every overwrite.
        sftp = self._require()
        try:
            attr = sftp.lstat(path)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        is_link = stat.S_ISLNK(attr.st_mode or 0)
        if is_link:
            try:
                attr = sftp.stat(path)
            except Exception:
                pass  # dangling link: report the link itself
        mode = attr.st_mode or 0
        return RemoteStat(
            path=path,
            is_dir=stat.S_ISDIR(mode),
            size=int(attr.st_size or 0),
            modified=float(attr.st_mtime) if attr.st_mtime else None,
            mode=stat.S_IMODE(mode) if mode else None,
            is_link=is_link,
        )

    # ----- mutations ------------------------------------------------------
    def mkdir(self, path: str) -> None:
        try:
            self._require().mkdir(path)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def remove(self, path: str) -> None:
        try:
            self._require().remove(path)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def rmdir(self, path: str) -> None:
        try:
            self._require().rmdir(path)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def rename(self, source: str, target: str) -> None:
        try:
            self._require().rename(source, target)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def replace(self, source: str, target: str) -> None:
        """Rename over an existing file, atomically where the server allows it.

        OpenSSH's ``posix-rename`` extension replaces the target in one step,
        which is what makes an atomic upload atomic. Servers without it fall
        back to unlink-then-rename.
        """
        sftp = self._require()
        if self._posix_rename:
            try:
                sftp.posix_rename(source, target)
                return
            except Exception:
                # Either the extension is missing or this server refused it;
                # do not pay for the attempt again on this connection.
                self._posix_rename = False
        super().replace(source, target)

    def chmod(self, path: str, mode: int) -> None:
        try:
            self._require().chmod(path, mode & 0o7777)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def readlink(self, path: str) -> str:
        try:
            return self._require().readlink(path) or ""
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def symlink(self, target: str, link_path: str) -> None:
        try:
            self._require().symlink(target, link_path)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def set_mtime(self, path: str, mtime: float) -> None:
        try:
            self._require().utime(path, (mtime, mtime))
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    # ----- transfers ------------------------------------------------------
    def download(
        self, remote: str, local: str, progress: ProgressCallback | None = None
    ) -> None:
        sftp = self._require()
        try:
            sftp.get(remote, local, callback=_adapt(progress))
        except Exception as exc:
            try:
                os.unlink(local)
            except OSError:
                pass
            raise TransferError(_describe(exc)) from exc

    def upload(
        self, local: str, remote: str, progress: ProgressCallback | None = None
    ) -> None:
        """Send one file.

        ``confirm=False`` deliberately: Paramiko's default follows every upload
        with a stat to compare the size, which is a whole round trip per file
        for something the write already reports. On a tree of small files that
        stat was a seventh of the entire deploy - the bytes are not what such a
        deploy spends its time on, the round trips are. A short write still
        raises, because closing the handle reports the server's status; and
        anyone who wants the file read back and compared has *Verify uploads*,
        which is a real check rather than a size that matches by luck.
        """
        sftp = self._require()
        try:
            sftp.put(local, remote, callback=_adapt(progress), confirm=False)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def stream_download(self, remote: str, sink, progress=None) -> int:
        """Read a remote file straight through, without keeping a copy."""
        sftp = self._require()
        total = 0
        try:
            size = int(sftp.stat(remote).st_size or 0)
        except Exception:
            size = 0
        try:
            with sftp.open(remote, "rb") as handle:
                if size:
                    handle.prefetch(size)  # Pipeline the reads; SFTP is chatty.
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    sink(chunk)
                    if progress is not None:
                        progress(total, size)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        return total

    # ----- running commands ----------------------------------------------
    def exec_command(self, command: str, *, timeout: float = 60.0) -> ExecResult:
        client = self._require_client()
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        return ExecResult(command=command, exit_status=status, stdout=out, stderr=err)

    def exec_stream(self, command: str) -> RemoteStream:
        client = self._require_client()
        transport = client.get_transport()
        if transport is None:
            raise TransferError(NOT_CONNECTED)
        try:
            channel = transport.open_session(timeout=TIMEOUT)
            channel.exec_command(command)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        return _ChannelStream(channel, command)

    def open_shell(self, *, width: int = 120, height: int = 32) -> ShellChannel:
        client = self._require_client()
        try:
            channel = client.invoke_shell(term="xterm", width=width, height=height)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc
        return _ShellStream(channel, "shell")


def _adapt(progress: ProgressCallback | None):
    """Paramiko's callback signature already matches ProgressCallback."""
    if progress is None:
        return None

    def relay(transferred: int, total: int) -> None:
        progress(int(transferred), int(total))

    return relay


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
