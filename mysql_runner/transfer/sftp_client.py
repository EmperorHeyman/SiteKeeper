"""SFTP backend built on Paramiko.

Host keys live in %APPDATA%\\Sitekeeper\\known_hosts. A key that has *changed*
always aborts the connection; a key never seen before is either recorded
silently or handed back as a question, depending on whether there is anybody
there to ask - see ``transfer/hostkeys.py``.

Authentication is whatever the account actually uses: a password, a key file
(with the stored password serving as its passphrase), an SSH agent, or the keys
in ``~/.ssh``. The agent is the one that used to be missing entirely, which
meant anyone who never types a password could not connect at all.

The server need not be reachable directly, either. A connection can go through
a jump host - another stored profile, so its credentials are already in the
vault - or through a ProxyCommand, which is how anything stranger gets in.

SFTP rides on SSH, so this backend can do everything the protocol allows and
then some: permissions, symlinks, timestamps, atomic renames, and running
commands on the server for the archive, search and disk-usage tools.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass

from mysql_runner.transfer import hostkeys
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
from mysql_runner.transfer.sshagent import open_agent


@dataclass(frozen=True)
class JumpHost:
    """A bastion to reach the real server through.

    Plain data rather than a profile, so it crosses threads with the rest of a
    connection spec and so the backends stay clear of the storage layer.
    """

    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    private_key_path: str = ""
    use_agent: bool = True
    label: str = ""


#: Timeout for the TCP/handshake phase, in seconds.
TIMEOUT = 20

#: How long to wait for one read from a streaming command before giving the
#: caller a turn (it may want to stop, or update a UI).
STREAM_POLL = 0.2

#: Seconds allowed for the "can this account run commands at all" probe.
PROBE_TIMEOUT = 6.0

#: How long the SFTP channel may hear *nothing at all* before a read gives
#: up, in seconds. Paramiko sets no read timeout, so a session that stops
#: answering - a dropped link the TCP stack has not noticed, or a channel
#: whose replies went astray - blocks its caller forever. Since that caller
#: is usually the worker thread, and the worker thread is what serves the
#: whole tab, "forever" meant a tab that quietly stopped doing anything at
#: all until it was closed and reopened. A failure is recoverable; a hang is
#: not - alive() then reports the session dead and it is reopened. This is
#: idle time, not total time: a transfer of any size keeps resetting it, so
#: it is generous enough that only a genuinely silent server trips it.
IDLE_TIMEOUT = 120.0

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
        use_agent: bool = True,
        use_default_keys: bool = False,
        host_key_mode: str = hostkeys.AUTO,
        jump: "JumpHost | None" = None,
        proxy_command: str = "",
    ) -> None:
        self._host = host
        self._port = port or 22
        self._username = username
        self._password = password
        self._key_path = private_key_path
        self._use_agent = use_agent
        self._use_default_keys = use_default_keys
        self._host_key_mode = host_key_mode
        self._jump = jump
        self._proxy_command = proxy_command
        self._client = None
        self._sftp = None
        self._jump_client = None   # the bastion, when connecting through one
        self._proxy_sock = None    # a ProxyCommand's process, when there is one
        self._posix_rename = True  # Until a server says otherwise.
        self._caps = _CAPABILITIES  # Narrowed by the probe in connect().

    # ----- lifecycle ------------------------------------------------------
    def connect(self) -> str:
        paramiko = import_paramiko()
        sock = self._open_route(paramiko)
        try:
            client = open_ssh_client(
                paramiko,
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                key_path=self._key_path,
                use_agent=self._use_agent,
                use_default_keys=self._use_default_keys,
                host_key_mode=self._host_key_mode,
                sock=sock,
            )
        except BaseException:
            self._close_route()
            raise

        transport = client.get_transport()
        try:
            if transport is None:
                raise TransferError("The SSH transport closed during login.")
            self._sftp = paramiko.SFTPClient.from_transport(
                transport, window_size=WINDOW_SIZE
            )
            if self._sftp is None:
                raise TransferError("The server refused to open an SFTP session.")
            channel = self._sftp.get_channel()
            if channel is not None:
                channel.settimeout(IDLE_TIMEOUT)
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

    # ----- getting there --------------------------------------------------
    def _open_route(self, paramiko):
        """The socket the SSH session runs over, or None to dial directly."""
        if self._proxy_command:
            command = (
                self._proxy_command
                .replace("%h", self._host)
                .replace("%p", str(self._port))
                .replace("%r", self._username or "")
            )
            try:
                self._proxy_sock = paramiko.ProxyCommand(command)
            except Exception as exc:
                raise TransferError(
                    f"The proxy command could not be started: {_describe(exc)}"
                ) from exc
            return self._proxy_sock
        if self._jump is None:
            return None
        return self._open_jump(paramiko)

    def _open_jump(self, paramiko):
        """Log in to the bastion, then ask it to reach the real server.

        ``direct-tcpip`` is the same thing OpenSSH's ``-J`` does: the bastion
        opens the second connection on our behalf and hands back a channel that
        behaves like a socket, so the SSH session to the real server is end to
        end and the bastion never sees its traffic in the clear.
        """
        jump = self._jump
        try:
            self._jump_client = open_ssh_client(
                paramiko,
                host=jump.host,
                port=jump.port or 22,
                username=jump.username,
                password=jump.password,
                key_path=jump.private_key_path,
                use_agent=jump.use_agent,
                use_default_keys=False,
                host_key_mode=self._host_key_mode,
                sock=None,
            )
        except TransferError as exc:
            where = jump.label or f"{jump.host}:{jump.port or 22}"
            raise TransferError(f"Jump host {where}: {exc}") from exc
        transport = self._jump_client.get_transport()
        if transport is None:
            raise TransferError("The jump host closed its connection.")
        try:
            return transport.open_channel(
                "direct-tcpip", (self._host, self._port), ("127.0.0.1", 0)
            )
        except Exception as exc:
            where = jump.label or jump.host
            raise TransferError(
                f"{where} would not open a connection to {self._host}:"
                f"{self._port}: {_channel_reason(exc)}"
            ) from exc

    def _close_route(self) -> None:
        """Close the bastion or proxy process, if there was one."""
        for attribute in ("_jump_client", "_proxy_sock"):
            handle = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass

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
        self._close_route()

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

    def supports_resume(self) -> bool:
        """Always: seeking a handle is part of the protocol, not an extension."""
        return True

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
        self,
        remote: str,
        local: str,
        progress: ProgressCallback | None = None,
        *,
        resume_from: int = 0,
        keep_partial: bool = False,
    ) -> None:
        sftp = self._require()
        try:
            if resume_from > 0:
                self._resume_download(sftp, remote, local, progress, resume_from)
            else:
                sftp.get(remote, local, callback=_adapt(progress))
        except Exception as exc:
            if not keep_partial:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            raise TransferError(_describe(exc)) from exc

    def _resume_download(self, sftp, remote, local, progress, resume_from) -> None:
        """Fetch the tail of a file whose first bytes are already on disk.

        Paramiko's ``get`` always starts at zero, so this is the read loop it
        would have run, seeked forward on both ends. The prefetch still matters
        - SFTP reads are chatty enough that without one a resumed copy crawls -
        and it is asked for from the seek position onwards.
        """
        try:
            size = int(sftp.stat(remote).st_size or 0)
        except Exception:
            size = 0
        if size and resume_from >= size:
            return  # already whole; nothing left to fetch
        transferred = resume_from
        with sftp.open(remote, "rb") as handle:
            handle.seek(resume_from)
            if size > resume_from:
                handle.prefetch(size)
            with open(local, "r+b") as sink:
                # Truncate as well as seek: a partial file whose tail is
                # garbage from a half-written chunk must not survive under
                # the bytes about to be appended.
                sink.seek(resume_from)
                sink.truncate(resume_from)
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    sink.write(chunk)
                    transferred += len(chunk)
                    if progress is not None:
                        progress(transferred, size)

    def upload(
        self,
        local: str,
        remote: str,
        progress: ProgressCallback | None = None,
        *,
        resume_from: int = 0,
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
            if resume_from > 0:
                self._resume_upload(sftp, local, remote, progress, resume_from)
            else:
                sftp.put(local, remote, callback=_adapt(progress), confirm=False)
        except Exception as exc:
            raise TransferError(_describe(exc)) from exc

    def _resume_upload(self, sftp, local, remote, progress, resume_from) -> None:
        """Append the rest of a local file to what already reached the server."""
        total = os.path.getsize(local)
        if resume_from >= total:
            return  # everything is already there
        transferred = resume_from
        with sftp.open(remote, "r+b") as handle:
            # Pipelining is what makes a write loop keep up with put(): without
            # it every 32 KB waits for its own acknowledgement.
            handle.set_pipelined(True)
            handle.seek(resume_from)
            handle.truncate(resume_from)
            with open(local, "rb") as source:
                source.seek(resume_from)
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    transferred += len(chunk)
                    if progress is not None:
                        progress(transferred, total)

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


def open_ssh_client(
    paramiko,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    key_path: str = "",
    use_agent: bool = True,
    use_default_keys: bool = False,
    host_key_mode: str = hostkeys.AUTO,
    sock=None,
):
    """Log in to one SSH server and return the connected client.

    Shared by the session itself and by its jump host, which needs exactly the
    same treatment - known hosts, agent, key file, password - and used to have
    none of it because there was no jump host.

    Paramiko builds its own ``Agent()`` only when ``client._agent`` is still
    None, so assigning one is how the keys from *every* agent on this machine
    get offered, including the Windows one Paramiko cannot reach by itself.
    """
    client = paramiko.SSHClient()
    hosts_file = hostkeys.known_hosts_path()
    if hosts_file.exists():
        try:
            client.load_host_keys(str(hosts_file))
        except (OSError, paramiko.SSHException):
            # A corrupt known_hosts must not lock somebody out of their own
            # servers; whatever is unreadable is written again below.
            pass
    client.set_missing_host_key_policy(hostkeys.policy(host_key_mode))

    agent = open_agent() if use_agent else None
    if agent is not None:
        client._agent = agent  # noqa: SLF001 - Paramiko's own extension point
    if not (key_path or password or agent or use_default_keys):
        # Asked before dialling, because Paramiko's own answer to this is
        # "No authentication methods available", which describes the library's
        # position rather than the user's problem - and it costs a round trip
        # to arrive at.
        raise TransferError(
            f"{username or 'This connection'} has nothing to log in with: no "
            "password, no private key, and no SSH agent running. Set one in "
            "the connection's settings, or start your agent and add the key."
        )

    kwargs: dict[str, object] = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": TIMEOUT,
        "allow_agent": bool(use_agent),
        "look_for_keys": bool(use_default_keys),
    }
    if sock is not None:
        kwargs["sock"] = sock
    if key_path:
        kwargs["key_filename"] = key_path
        # A passphrase-protected key reuses the stored password field.
        if password:
            kwargs["passphrase"] = password
    elif password:
        # Only when there is one: passing an empty password spends an
        # authentication attempt that cannot succeed, and servers with a low
        # MaxAuthTries then refuse the agent key that would have worked.
        kwargs["password"] = password

    try:
        client.connect(**kwargs)  # type: ignore[arg-type]
    except paramiko.BadHostKeyException as exc:
        _discard_agent(agent)
        raise TransferError(
            f"The host key for {host} does not match the one recorded the "
            "last time you connected. If this server was legitimately "
            "rekeyed, forget its old key in the connection's settings and "
            "connect again; if it was not, do not connect."
        ) from exc
    except hostkeys.HostKeyUnknown:
        _discard_agent(agent)
        raise  # the caller decides whether to trust it
    except paramiko.AuthenticationException as exc:
        _discard_agent(agent)
        raise TransferError(_auth_failure(username, key_path, password, agent, use_default_keys)) from exc
    except paramiko.SSHException as exc:
        _discard_agent(agent)
        if "no authentication methods" in str(exc).lower():
            # The same problem as above, reached by a different road: every
            # credential offered was rejected before it could be tried.
            raise TransferError(
                _auth_failure(username, key_path, password, agent, use_default_keys)
            ) from exc
        raise TransferError(_describe(exc)) from exc
    except OSError as exc:
        _discard_agent(agent)
        raise TransferError(_describe(exc)) from exc

    try:
        client.save_host_keys(str(hosts_file))
    except (OSError, paramiko.SSHException):
        # Persisting the key is a convenience; carry on without it.
        pass
    return client


def _discard_agent(agent) -> None:
    """Close an agent opened for a login that did not happen."""
    if agent is None:
        return
    try:
        agent.close()
    except Exception:
        pass


def _auth_failure(
    username: str, key_path: str, password: str, agent, use_default_keys: bool
) -> str:
    """Say what was actually offered, because "Authentication failed" does not.

    Three different problems used to arrive under one sentence: the wrong
    password, a key the server does not have, and an agent that is not running.
    Naming what was tried turns each of them into something to go and check.
    """
    tried = []
    if key_path:
        tried.append(f"the key file {os.path.basename(key_path)}")
    if agent is not None:
        tried.append(f"keys from your SSH agent - {agent.describe()}")
    if use_default_keys:
        tried.append("the keys in ~/.ssh")
    if password and not key_path:
        tried.append("the stored password")
    if not tried:
        return (
            f"{username or 'This account'} was refused, and there was nothing "
            "to offer: no password, no key file, and no SSH agent running. "
            "Set a password or a private key in the connection's settings, or "
            "start your agent and add the key."
        )
    return (
        f"{username or 'That account'} was refused. Tried "
        + ", ".join(tried)
        + "."
    )


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


def _channel_reason(exc: Exception) -> str:
    """The server's own words when it refuses to open a channel.

    Paramiko's ChannelException stringifies as ``ChannelException(1, '...')``,
    which puts a Python repr in front of somebody whose only problem is that
    their bastion has ``AllowTcpForwarding no``. The text inside it is the
    part that means something.
    """
    text = getattr(exc, "text", "")
    if text:
        return str(text)
    return _describe(exc)
