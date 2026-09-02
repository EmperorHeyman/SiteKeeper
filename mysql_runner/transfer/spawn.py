"""Launch an external terminal into the directory you are looking at.

An embedded shell covers most of what you need, but sometimes you want the real
thing - PuTTY, Windows Terminal, whatever you have configured. This builds the
right command line for each, including the host, port, credentials and a ``cd``
into the current remote directory.

A note on passwords: PuTTY and its forks take one on the command line, which
means it is briefly visible to anything that can list processes on this machine.
That is what makes the feature useful (it is exactly what WinSCP does), but it
is a real trade-off, so passing the password is opt-in per launch and a key file
is always preferred when the profile has one.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from enum import Enum

from mysql_runner.transfer import shellaccess


class TerminalKind(str, Enum):
    """How to talk to a given terminal program."""

    PUTTY = "putty"          # putty.exe / kitty.exe: -ssh, -P, -pw, -m
    OPENSSH = "openssh"      # ssh.exe: user@host with a remote command
    WT = "wt"                # Windows Terminal wrapping ssh
    WSL = "wsl"              # wsl.exe wrapping ssh


@dataclass(frozen=True)
class Terminal:
    """One terminal program found on this machine."""

    name: str
    executable: str
    kind: TerminalKind

    @property
    def available(self) -> bool:
        return bool(self.executable) and os.path.isfile(self.executable)


@dataclass(frozen=True)
class ShellTarget:
    """Where the terminal should connect, and where it should land."""

    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    key_path: str = ""
    remote_dir: str = ""

    def user_at_host(self) -> str:
        return f"{self.username}@{self.host}" if self.username else self.host


#: Where each program usually lives, beyond whatever is on PATH.
_CANDIDATES = (
    ("PuTTY", TerminalKind.PUTTY, (
        r"C:\Program Files\PuTTY\putty.exe",
        r"C:\Program Files (x86)\PuTTY\putty.exe",
    ), "putty.exe"),
    ("KiTTY", TerminalKind.PUTTY, (
        r"C:\Program Files\KiTTY\kitty.exe",
        r"C:\Program Files (x86)\KiTTY\kitty.exe",
    ), "kitty.exe"),
    ("Windows Terminal", TerminalKind.WT, (), "wt.exe"),
    ("OpenSSH", TerminalKind.OPENSSH, (
        r"C:\Windows\System32\OpenSSH\ssh.exe",
    ), "ssh.exe"),
    ("WSL", TerminalKind.WSL, (), "wsl.exe"),
)


def which(program: str) -> str:
    """Full path to ``program`` on PATH, or "" when it is not there."""
    from shutil import which as _which

    return _which(program) or ""


def detect_terminals() -> list[Terminal]:
    """Every supported terminal present on this machine, best first."""
    found: list[Terminal] = []
    for name, kind, fixed_paths, program in _CANDIDATES:
        path = which(program)
        if not path:
            path = next((candidate for candidate in fixed_paths if os.path.isfile(candidate)), "")
        if path:
            found.append(Terminal(name=name, executable=path, kind=kind))
    return found


def preferred_terminal(name: str = "") -> "Terminal | None":
    """The terminal to use: the configured one, else the best one present.

    None means this machine has none of them, which is worth saying rather
    than failing to start something.
    """
    terminals = detect_terminals()
    if not terminals:
        return None
    return next((item for item in terminals if item.name == name), terminals[0])


def target_for(profile, remote_dir: str = "") -> ShellTarget:
    """Where a terminal for this saved connection should log in.

    The port comes from :func:`shellaccess.shell_port`, not from the profile:
    on an FTP connection the profile's port is the FTP one, and handing that
    to ssh would dial the file-transfer service and hang.
    """
    return ShellTarget(
        host=profile.host,
        port=shellaccess.shell_port(profile),
        username=profile.username,
        password=profile.password,
        key_path=profile.private_key_path,
        remote_dir=remote_dir,
    )


def remote_login_command(remote_dir: str) -> str:
    """The shell command that lands you in ``remote_dir`` and stays there."""
    if not remote_dir:
        return "exec $SHELL -l"
    return f"cd {shlex.quote(remote_dir)} 2>/dev/null; exec $SHELL -l"


def build_command(
    terminal: Terminal,
    target: ShellTarget,
    *,
    include_password: bool = True,
    session_file: str = "",
) -> list[str]:
    """The argv to start ``terminal`` connected to ``target``.

    ``session_file`` is a local file holding the commands PuTTY should run on
    login; :func:`write_session_file` produces one. PuTTY has no way to pass a
    remote command inline, so landing in the right directory needs it.
    """
    if terminal.kind == TerminalKind.PUTTY:
        argv = [terminal.executable, "-ssh", target.user_at_host()]
        if target.port:
            argv += ["-P", str(target.port)]
        if target.key_path:
            argv += ["-i", target.key_path]
        elif include_password and target.password:
            argv += ["-pw", target.password]
        if session_file:
            argv += ["-t", "-m", session_file]
        return argv

    if terminal.kind == TerminalKind.WT:
        inner = _ssh_argv("ssh.exe", target)
        return [terminal.executable, "new-tab", "--title", target.host, *inner]

    if terminal.kind == TerminalKind.WSL:
        inner = _ssh_argv("ssh", target)
        return [terminal.executable, "--", *inner]

    return _ssh_argv(terminal.executable, target)


def _ssh_argv(executable: str, target: ShellTarget) -> list[str]:
    argv = [executable, "-t"]
    if target.port:
        argv += ["-p", str(target.port)]
    if target.key_path:
        argv += ["-i", target.key_path]
    argv.append(target.user_at_host())
    argv.append(remote_login_command(target.remote_dir))
    return argv


def write_session_file(target: ShellTarget, directory: str = "") -> str:
    """Write the login script PuTTY runs, and return its path.

    Kept in the temp directory and overwritten per launch; it contains a path,
    never a credential.
    """
    import tempfile

    folder = directory or tempfile.gettempdir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "mysqlrunner-login.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(remote_login_command(target.remote_dir) + "\n")
    return path


def launch(
    terminal: Terminal,
    target: ShellTarget,
    *,
    include_password: bool = True,
) -> subprocess.Popen:
    """Start the terminal. Raises OSError when the program will not run."""
    session_file = ""
    if terminal.kind == TerminalKind.PUTTY and target.remote_dir:
        session_file = write_session_file(target)
    argv = build_command(
        terminal, target, include_password=include_password, session_file=session_file
    )
    creation_flags = 0
    if os.name == "nt":
        # Detach so closing the app does not take the terminal with it.
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    return subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
        argv,
        creationflags=creation_flags if os.name == "nt" else 0,
        close_fds=True,
    )


def describe_command(argv: list[str], *, redact: str = "") -> str:
    """A printable form of the command, with the password blanked out."""
    parts: list[str] = []
    for index, value in enumerate(argv):
        shown = value
        if redact and value == redact:
            shown = "********"
        elif index > 0 and argv[index - 1] == "-pw":
            shown = "********"
        parts.append(f'"{shown}"' if " " in shown else shown)
    return " ".join(parts)
