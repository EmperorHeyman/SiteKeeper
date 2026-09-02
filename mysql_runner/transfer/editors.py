"""Hand a file - or a whole server directory - to VS Code.

Two different things wear the same name in the menu, and the difference is the
whole point of the module:

* **A remote file.** Downloaded to a scratch copy and opened in the editor,
  with every save uploaded back - the loop the file manager already runs for
  "Edit locally", pointed at a named program instead of whatever Windows has
  registered for ``.php``. Works on FTP, FTPS and SFTP alike, because it is
  only ever a local file as far as the editor is concerned.
* **A remote folder.** Handed to VS Code's Remote-SSH extension, which opens
  its own SSH session and edits the files in place. No download, no scratch
  copy, and the editor's search, git and terminal are the server's. SSH only:
  there is no FTP authority, and inventing one would fail at the point of use.

Remote-SSH authenticates itself. It never sees this app's stored password, so a
connection with no key and no agent will make VS Code ask for one - which is
the honest trade (a password on a command line is visible to anything that can
list processes, see ``spawn.py``), but worth saying in the UI rather than
letting a prompt appear out of nowhere.

Everything here is Qt-free and every command line is built by a pure function,
so the argv a launch would use can be asserted on without starting anything.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from mysql_runner.transfer.spawn import which

#: Editors that speak VS Code's command line. All of them take a path to open,
#: ``--remote <authority> <path>`` to open one on a server, and ``--goto
#: file:line`` - they are the same CLI, so one launcher covers the family.
#:
#: Per entry: the display name, the programs to look for on PATH, and the
#: fixed places to look when PATH has nothing. The fixed paths point at the
#: *CLI* (``bin\\code.cmd``, or ``bin/code`` on the Mac) rather than the
#: application binary: it is the documented interface, it is the one certain to
#: understand ``--remote``, and a console window it would flash is suppressed
#: at launch instead.
_CANDIDATES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Visual Studio Code",
        ("code",),
        (
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd",
            r"%PROGRAMFILES%\Microsoft VS Code\bin\code.cmd",
            r"%PROGRAMFILES(X86)%\Microsoft VS Code\bin\code.cmd",
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            "/usr/share/code/bin/code",
            "/snap/bin/code",
            "/usr/bin/code",
        ),
    ),
    (
        "VS Code Insiders",
        ("code-insiders",),
        (
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code Insiders\bin\code-insiders.cmd",
            r"%PROGRAMFILES%\Microsoft VS Code Insiders\bin\code-insiders.cmd",
            "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/"
            "app/bin/code-insiders",
            "/usr/share/code-insiders/bin/code-insiders",
        ),
    ),
    (
        "Cursor",
        ("cursor",),
        (
            r"%LOCALAPPDATA%\Programs\cursor\resources\app\bin\cursor.cmd",
            r"%PROGRAMFILES%\cursor\resources\app\bin\cursor.cmd",
            "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        ),
    ),
    (
        "VSCodium",
        ("codium",),
        (
            r"%LOCALAPPDATA%\Programs\VSCodium\bin\codium.cmd",
            r"%PROGRAMFILES%\VSCodium\bin\codium.cmd",
            "/Applications/VSCodium.app/Contents/Resources/app/bin/codium",
            "/usr/share/codium/bin/codium",
            "/usr/bin/codium",
        ),
    ),
    (
        "Windsurf",
        ("windsurf",),
        (
            r"%LOCALAPPDATA%\Programs\Windsurf\bin\windsurf.cmd",
            r"%PROGRAMFILES%\Windsurf\bin\windsurf.cmd",
            "/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf",
        ),
    ),
)


@dataclass(frozen=True)
class Editor:
    """One VS Code-family editor found on this machine."""

    name: str
    executable: str

    @property
    def available(self) -> bool:
        return bool(self.executable) and os.path.isfile(self.executable)

    @property
    def is_script(self) -> bool:
        """Whether the executable is a batch wrapper around the real binary.

        ``code.cmd`` is a two-line script that runs the app in CLI mode; run
        as-is it flashes a console window on top of whatever you were doing,
        so :func:`launch` asks Windows not to give it one.
        """
        return self.executable.lower().endswith((".cmd", ".bat"))


@dataclass(frozen=True)
class RemoteTarget:
    """The server a Remote-SSH window should attach to."""

    host: str
    port: int = 22
    username: str = ""

    def authority(self) -> str:
        """The ``ssh-remote+…`` authority VS Code names a server by.

        The port is left out when it is the default, because that is the form
        every Remote-SSH document and every ``~/.ssh/config`` host uses, and a
        redundant ``:22`` is one more thing for the extension to disagree
        about.
        """
        base = f"{self.username}@{self.host}" if self.username else self.host
        if self.port and self.port != 22:
            return f"ssh-remote+{base}:{self.port}"
        return f"ssh-remote+{base}"


def detect_editors() -> list[Editor]:
    """Every supported editor present on this machine, best first."""
    found: list[Editor] = []
    for name, programs, fixed_paths in _CANDIDATES:
        path = ""
        for program in programs:
            path = which(program)
            if path:
                break
        if not path:
            path = next(
                (
                    candidate
                    for candidate in (
                        os.path.expandvars(raw) for raw in fixed_paths
                    )
                    if os.path.isfile(candidate)
                ),
                "",
            )
        if path:
            found.append(Editor(name=name, executable=path))
    return found


def find_editor(preferred: str = "") -> Editor | None:
    """The editor to use: the one asked for by name, else the first found.

    A preference that is no longer installed does not silently become a
    different editor - it is a stale setting, and answering with the wrong
    program is worse than answering with none.
    """
    editors = detect_editors()
    if preferred:
        return next((item for item in editors if item.name == preferred), None)
    return editors[0] if editors else None


def open_argv(editor: Editor, paths: list[str], *, new_window: bool = False) -> list[str]:
    """The argv that opens local ``paths`` in ``editor``.

    A folder opens as a workspace and a file opens in the last window used -
    VS Code's own rules, which are the ones the user already expects from
    Explorer's *Open with Code*.
    """
    argv = [editor.executable]
    if new_window:
        argv.append("-n")
    argv.extend(paths)
    return argv


def remote_argv(editor: Editor, target: RemoteTarget, remote_path: str) -> list[str]:
    """The argv that opens ``remote_path`` on ``target`` over Remote-SSH."""
    return [editor.executable, "--remote", target.authority(), remote_path or "/"]


def launch(argv: list[str], *, script: bool = False) -> subprocess.Popen:
    """Start the editor. Raises OSError when the program will not run.

    Not detached the way a terminal is: VS Code re-parents itself to its own
    window, and closing this app has never taken it with it.
    """
    creation_flags = 0
    if os.name == "nt" and script:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(  # noqa: S603 - argv is built here, never a shell string
        argv,
        creationflags=creation_flags if os.name == "nt" else 0,
        close_fds=True,
    )


def open_paths(editor: Editor, paths: list[str], *, new_window: bool = False):
    """Open local files or folders. Convenience over :func:`open_argv`."""
    return launch(
        open_argv(editor, paths, new_window=new_window), script=editor.is_script
    )


def open_remote(editor: Editor, target: RemoteTarget, remote_path: str):
    """Open a server directory in place. Convenience over :func:`remote_argv`."""
    return launch(remote_argv(editor, target, remote_path), script=editor.is_script)
