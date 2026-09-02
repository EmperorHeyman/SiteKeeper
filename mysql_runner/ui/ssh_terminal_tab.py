"""An embedded SSH shell, opened in the directory you were looking at.

This is a line-oriented console, not a full terminal emulator: you type a
command, it goes to the shell, and the output appears. That covers what a file
manager's shell is actually for - a quick ``composer install``, a ``git pull``,
a look at a config - without pretending to be a replacement for a real terminal
(there is a button for that too, see ``transfer/spawn.py``).

Being line-oriented is what makes the rest of this possible. In a real terminal
the *remote* shell owns the line you are typing, so the client can only forward
keystrokes and hope; here the line belongs to this window until you press Enter,
which means it can be helped:

* **It remembers.** Commands are kept per connection and survive closing the
  tab, the app and the machine (``transfer/shellhistory.py``).
* **It suggests.** As you type, the rest of the most recent matching command
  appears in front of the cursor; Right or End takes it.
* **Up walks what you started typing.** Type ``git`` and Up walks the git
  commands, not everything that happened to come before them.
* **Tab completes remote paths** - over the same SSH connection, so it is the
  real directory listing rather than a guess - and command names you have used.
* **Ctrl+R searches backwards** through the history, the way a shell does.
* **A destructive command on a production server asks first.**

The shell runs on its own SSH connection so nothing here can stall the file
panes, and colour escape sequences are stripped so the transcript stays
readable and copyable.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import time

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage.models import Environment, ServerProfile
from mysql_runner.storage.settings import Settings
from mysql_runner.transfer import hostkeys
from mysql_runner.transfer.base import TransferError
from mysql_runner.transfer.shellhistory import ShellHistory
from mysql_runner.transfer.worker import ConnectionSpec
from mysql_runner.ui import theme

#: How often to look for new output, in milliseconds.
POLL_MS = 80

#: Keep the transcript bounded so a runaway command cannot exhaust memory.
MAX_BLOCKS = 20_000

#: How long a directory listing fetched for Tab-completion stays usable.
#: Long enough that pressing Tab twice is instant, short enough that a file
#: created a moment ago by the command before is still found.
COMPLETION_TTL = 10.0

#: Beyond this many matches, Tab lists them rather than filling the line.
COMPLETION_LIST_LIMIT = 200

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]|[\x00\x07\x0f]")

#: Keys that are only ever held, never typed. They must not be mistaken for
#: input - see _CommandLine.keyPressEvent.
_MODIFIER_KEYS = frozenset(
    {
        Qt.Key.Key_Control,
        Qt.Key.Key_Shift,
        Qt.Key.Key_Alt,
        Qt.Key.Key_AltGr,
        Qt.Key.Key_Meta,
        Qt.Key.Key_CapsLock,
        Qt.Key.Key_NumLock,
    }
)

#: Commands worth a second look before they run on a live server. Each is a
#: pattern and the plain-English thing it does, because "are you sure?" over a
#: command line nobody re-reads is not a safeguard, it is a keystroke.
_DESTRUCTIVE = (
    (re.compile(r"(?:^|[;&|]\s*)rm\s+(?:-\w+\s+)*-\w*[rf]", re.I),
     "delete files and folders outright"),
    (re.compile(r"\bdrop\s+(?:table|database|schema)\b", re.I),
     "drop a database or a table"),
    (re.compile(r"\btruncate\s+table\b", re.I), "empty a table"),
    (re.compile(r"\bchmod\s+(?:-R\s+)?777\b"), "make files writable by anyone"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-\w*f)", re.I),
     "throw away uncommitted changes"),
    (re.compile(r"\b(?:mkfs\b|dd\s+if=)", re.I), "overwrite a disk"),
    (re.compile(r">\s*/(?:etc|usr|var|boot)/"), "overwrite a system file"),
    (re.compile(r"\b(?:shutdown|reboot|halt)\b", re.I), "restart the server"),
)

#: Offered when completing the first word, alongside what you have actually
#: used here. Just the verbs a deploy shell reaches for.
_COMMON_COMMANDS = (
    "cat", "cd", "chmod", "chown", "composer", "cp", "curl", "df", "du",
    "find", "git", "grep", "head", "htop", "ls", "mkdir", "mv", "mysql",
    "mysqldump", "nano", "npm", "php", "ps", "pwd", "rm", "rsync", "systemctl",
    "tail", "tar", "top", "touch", "unzip", "wget", "which", "zip",
)


def strip_ansi(text: str) -> str:
    """Remove colour and cursor escapes, and normalise line endings."""
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def destructive_reason(command: str) -> str:
    """What a command would do that is worth confirming, or "" if nothing."""
    for pattern, description in _DESTRUCTIVE:
        if pattern.search(command):
            return description
    return ""


def token_at(text: str, position: int) -> tuple[str, int]:
    """The word being completed, and where in ``text`` it starts."""
    position = max(0, min(position, len(text)))
    start = position
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return text[start:position], start


def common_prefix(values: list[str]) -> str:
    """The longest start every candidate shares - what Tab fills in."""
    if not values:
        return ""
    shortest = min(values, key=len)
    for index, char in enumerate(shortest):
        if any(value[index] != char for value in values):
            return shortest[:index]
    return shortest


class _ShellWorker(QObject):
    """Owns the SSH channel and does the reading, off the GUI thread."""

    opened = pyqtSignal(str)
    output = pyqtSignal(str)
    failed = pyqtSignal(str)
    #: This server has never been confirmed. Carries a hostkeys.HostKeyUnknown
    #: for the tab to put to the user, the same way a file-manager tab does.
    host_key_unknown = pyqtSignal(object)
    finished = pyqtSignal()
    #: A directory listing asked for by Tab: (path, [(name, is_dir), ...]).
    listed = pyqtSignal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self._fs = None
        self._shell = None
        self._timer: QTimer | None = None

    @pyqtSlot(object, str)
    def open_shell(self, spec: object, cwd: str) -> None:
        assert isinstance(spec, ConnectionSpec)
        try:
            fs = spec.build()
            fs.connect()
            shell = fs.open_shell()
        except hostkeys.HostKeyUnknown as exc:
            # Not a failure - a question, and one this window reaches more
            # often than the file panes do: a terminal on an FTP connection
            # is the first time anything here has spoken SSH to that host, so
            # there is no key on file for it yet.
            self.host_key_unknown.emit(exc)
            return
        except TransferError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self._fs = fs
        self._shell = shell
        banner = f"Connected to {spec.host}."
        if cwd:
            # A plain cd, sent as if typed, so the shell's own prompt follows.
            self.send(f"cd {shlex.quote(cwd)}\n")
            banner += f" Starting in {cwd}."
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._drain)
        self._timer.start()
        self.opened.emit(banner)

    @pyqtSlot(str)
    def send(self, data: str) -> None:
        shell = self._shell
        if shell is None:
            self.failed.emit("The shell is not open.")
            return
        try:
            shell.send(data)
        except TransferError as exc:
            self.failed.emit(str(exc))

    @pyqtSlot(str)
    def list_names(self, path: str) -> None:
        """List one directory for Tab-completion.

        Over the session's own SFTP channel rather than by sending something
        to the shell: asking the shell would mean writing a command into the
        transcript the user did not type, and reading the answer back out of
        whatever else was arriving at the time.
        """
        fs = self._fs
        if fs is None:
            return
        try:
            entries = fs.listdir(path or "/")
        except (TransferError, OSError):
            self.listed.emit(path, [])
            return
        except Exception:
            self.listed.emit(path, [])
            return
        self.listed.emit(path, [(e.name, e.is_dir) for e in entries])

    @pyqtSlot(int, int)
    def resize(self, width: int, height: int) -> None:
        if self._shell is not None:
            self._shell.resize(width, height)

    @pyqtSlot()
    def close_shell(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._shell is not None:
            self._shell.close()
            self._shell = None
        if self._fs is not None:
            try:
                self._fs.close()
            except Exception:
                pass
            self._fs = None
        self.finished.emit()

    def _drain(self) -> None:
        shell = self._shell
        if shell is None:
            return
        try:
            text = shell.read_text(0.0)
        except Exception as exc:
            self.failed.emit(str(exc))
            self.close_shell()
            return
        if text:
            self.output.emit(text)
        elif not shell.active():
            self.output.emit("\n[the shell has exited]\n")
            self.close_shell()


class _CommandLine(QLineEdit):
    """The input line, and everything a shell's input line ought to do.

    The suggestion is drawn rather than inserted. Inserting it would mean the
    text you are looking at is not the text you typed - every keystroke would
    have to decide whether to keep or discard something you never asked for,
    and pressing Enter at the wrong moment would run it. Drawn in front of the
    cursor it is an offer: Right or End takes it, anything else ignores it.
    """

    completion_requested = pyqtSignal(str, int)
    search_state = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: ShellHistory | None = None
        self._ghost = ""
        self._ghost_colour = QColor("#6b7280")
        # Walking the history: what was typed before it started, what it has
        # to start with, and where in the matches we are.
        self._walk_prefix = ""
        self._walk_matches: list[str] = []
        self._walk_index = -1
        self._walk_saved = ""
        # Reverse search (Ctrl+R).
        self._search_active = False
        self._search_text = ""
        self._search_matches: list[str] = []
        self._search_index = 0
        self._search_saved = ""
        self.textEdited.connect(self._on_edited)

    # ----- wiring ---------------------------------------------------------
    def set_history(self, history: ShellHistory) -> None:
        self._history = history

    def set_ghost_colour(self, colour: str) -> None:
        self._ghost_colour = QColor(colour)
        self.update()

    def reset_walk(self) -> None:
        self._walk_matches = []
        self._walk_index = -1
        self._walk_prefix = ""
        self._walk_saved = ""

    # ----- the suggestion in front of the cursor --------------------------
    def _refresh_ghost(self) -> None:
        ghost = ""
        if (
            self._history is not None
            and not self._search_active
            and self.text()
            and self.cursorPosition() == len(self.text())
            and not self.hasSelectedText()
        ):
            ghost = self._history.suggest(self.text())
        if ghost != self._ghost:
            self._ghost = ghost
            self.update()

    def _on_edited(self, _text: str) -> None:
        self.reset_walk()
        self._refresh_ghost()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self._ghost:
            return
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        painter.setFont(self.font())
        painter.setPen(self._ghost_colour)
        # The cursor is at the end of the text whenever a ghost is showing,
        # so its rectangle is exactly where the rest of the command goes.
        cursor = self.cursorRect()
        baseline = cursor.top() + self.fontMetrics().ascent()
        painter.drawText(cursor.left() + 1, baseline, self._ghost)
        painter.end()

    def accept_suggestion(self) -> bool:
        """Take the offer. False when there was nothing on offer."""
        if not self._ghost:
            return False
        self.setText(self.text() + self._ghost)
        self.setCursorPosition(len(self.text()))
        self._ghost = ""
        self.reset_walk()
        self._refresh_ghost()
        return True

    # ----- walking the history -------------------------------------------
    def _walk(self, delta: int) -> None:
        if self._history is None:
            return
        if self._walk_index < 0:
            # Starting out: what is typed so far decides what we walk.
            self._walk_saved = self.text()
            self._walk_prefix = self.text()
            self._walk_matches = self._history.matching(self._walk_prefix)
            self._walk_index = -1
        if not self._walk_matches:
            return
        index = self._walk_index + delta
        if index < 0:
            return
        if index >= len(self._walk_matches):
            # Past the oldest match: stay there rather than emptying the line.
            index = len(self._walk_matches) - 1
        self._walk_index = index
        self._set_text_quietly(self._walk_matches[index])

    def _walk_forward(self) -> None:
        if self._walk_index < 0:
            return
        if self._walk_index == 0:
            self._walk_index = -1
            self._set_text_quietly(self._walk_saved)
            return
        self._walk_index -= 1
        self._set_text_quietly(self._walk_matches[self._walk_index])

    def _set_text_quietly(self, text: str) -> None:
        """Set the line without treating it as something the user typed."""
        self.blockSignals(True)
        self.setText(text)
        self.blockSignals(False)
        self.setCursorPosition(len(text))
        self._ghost = ""
        self.update()

    # ----- reverse search -------------------------------------------------
    def _begin_search(self) -> None:
        if self._history is None:
            return
        if not self._search_active:
            self._search_active = True
            self._search_saved = self.text()
            self._search_text = ""
            self._search_matches = []
            self._search_index = 0
            self._ghost = ""
        else:
            # Ctrl+R again: the match before this one.
            if self._search_index + 1 < len(self._search_matches):
                self._search_index += 1
        self._apply_search()

    def _apply_search(self) -> None:
        if self._history is None:
            return
        self._search_matches = self._history.search(self._search_text)
        if self._search_index >= len(self._search_matches):
            self._search_index = max(0, len(self._search_matches) - 1)
        if self._search_matches:
            self._set_text_quietly(self._search_matches[self._search_index])
            position = f" [{self._search_index + 1}/{len(self._search_matches)}]"
        else:
            position = " — nothing found" if self._search_text else ""
        self.search_state.emit(
            f"(reverse search) '{self._search_text}'{position}"
            "     Enter keeps it · Esc cancels · Ctrl+R for the one before"
        )

    def _end_search(self, *, keep: bool) -> None:
        if not self._search_active:
            return
        self._search_active = False
        if not keep:
            self._set_text_quietly(self._search_saved)
        self._search_text = ""
        self._search_matches = []
        self._search_index = 0
        self.search_state.emit("")
        self._refresh_ghost()

    @property
    def searching(self) -> bool:
        return self._search_active

    # ----- keys -----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        control = event.modifiers() & Qt.KeyboardModifier.ControlModifier

        if key in _MODIFIER_KEYS:
            # Holding Ctrl arrives as a key press in its own right, before the
            # key it modifies. Treated as input it ended the reverse search
            # that Ctrl+R had just begun, so a second Ctrl+R started a fresh
            # search instead of stepping back to the match before.
            super().keyPressEvent(event)
            return

        if control and key == Qt.Key.Key_R:
            self._begin_search()
            return

        if self._search_active:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # Keep the match on the line rather than running it. One more
                # Enter runs it, which is the difference between recalling a
                # command and firing it at a server by muscle memory.
                self._end_search(keep=True)
                return
            if key == Qt.Key.Key_Escape:
                self._end_search(keep=False)
                return
            if key == Qt.Key.Key_Backspace:
                self._search_text = self._search_text[:-1]
                self._search_index = 0
                self._apply_search()
                return
            if event.text() and event.text().isprintable():
                self._search_text += event.text()
                self._search_index = 0
                self._apply_search()
                return
            self._end_search(keep=True)
            # and fall through to handle the key normally

        if key == Qt.Key.Key_Up:
            self._walk(1)
            return
        if key == Qt.Key.Key_Down:
            self._walk_forward()
            return
        if key == Qt.Key.Key_Tab:
            self.completion_requested.emit(self.text(), self.cursorPosition())
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_End) and self._ghost:
            if self.cursorPosition() == len(self.text()):
                self.accept_suggestion()
                return
        if key == Qt.Key.Key_Escape:
            self.clear()
            self.reset_walk()
            self._refresh_ghost()
            return
        super().keyPressEvent(event)
        self._refresh_ghost()


class SshTerminalTab(QWidget):
    """A shell session on the server, as a tab."""

    status_message = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    _open_requested = pyqtSignal(object, str)
    _send_requested = pyqtSignal(str)
    _list_requested = pyqtSignal(str)
    _close_requested = pyqtSignal()

    def __init__(
        self,
        profile: ServerProfile,
        spec: ConnectionSpec,
        cwd: str = "",
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._settings = settings or Settings()
        #: Kept so the connection can be made a second time after the host
        #: key has been confirmed - the directory as it was asked for, not
        #: _cwd's "/" fallback, so a retry lands where the first try would
        #: have.
        self._spec = spec
        self._start_dir = cwd
        self._cwd = cwd or "/"
        self._closing = False
        self._history = ShellHistory(profile.id)
        #: path -> (when it was fetched, [(name, is_dir), ...])
        self._listings: dict[str, tuple[float, list]] = {}
        #: What Tab is waiting for: (directory, partial, token start).
        self._pending: tuple[str, str, int] | None = None

        self._build_ui()
        self.set_dark_mode(dark_mode)
        self._start_worker()
        self._open_requested.emit(spec, cwd)

    # ----- UI -------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setMaximumBlockCount(MAX_BLOCKS)
        self._transcript.setFont(theme.mono_font())
        self._transcript.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._transcript, 1)

        row = QHBoxLayout()
        if self._profile.environment == Environment.PROD:
            # Beside the command line, where it is in view while you type.
            row.addWidget(
                theme.production_badge("This shell runs on the live server.")
            )
        self._input = _CommandLine()
        self._input.setFont(theme.mono_font())
        self._input.setPlaceholderText("Connecting…")
        self._input.setEnabled(False)
        self._input.set_history(self._history)
        self._input.returnPressed.connect(self._on_submit)
        self._input.completion_requested.connect(self._on_complete)
        self._input.search_state.connect(self._on_search_state)
        interrupt = QPushButton("Ctrl+C")
        interrupt.setToolTip("Interrupt whatever is running")
        interrupt.clicked.connect(lambda: self._send_requested.emit("\x03"))
        clear = QPushButton("Clear")
        clear.clicked.connect(self._transcript.clear)
        row.addWidget(self._input, 1)
        row.addWidget(interrupt)
        row.addWidget(clear)
        layout.addLayout(row)

        self._status = QLabel("Opening a shell…")
        self._status.setObjectName("status")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        QShortcut(QKeySequence("Ctrl+L"), self, self._transcript.clear)

    def set_dark_mode(self, enable: bool) -> None:
        self.setStyleSheet(theme.console_stylesheet(enable))
        self._input.set_ghost_colour(theme.palette(enable).text_faint)

    def current_title(self) -> str:
        return f"{self._profile.label} — shell"

    @property
    def server_profile(self) -> ServerProfile:
        return self._profile

    # ----- worker ---------------------------------------------------------
    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = _ShellWorker()
        self._worker.moveToThread(self._thread)
        self._open_requested.connect(self._worker.open_shell)
        self._send_requested.connect(self._worker.send)
        self._list_requested.connect(self._worker.list_names)
        self._close_requested.connect(self._worker.close_shell)
        self._worker.opened.connect(self._on_opened)
        self._worker.output.connect(self._on_output)
        self._worker.failed.connect(self._on_failed)
        self._worker.host_key_unknown.connect(self._on_host_key_unknown)
        self._worker.finished.connect(self._on_finished)
        self._worker.listed.connect(self._on_listed)
        self._thread.start()

    def _on_opened(self, banner: str) -> None:
        self._say(banner)
        self._input.setEnabled(True)
        self._input.setPlaceholderText(
            "Type a command · Tab completes · ↑ recalls · Ctrl+R searches"
        )
        self._input.setFocus()
        self.status_message.emit(banner)
        self.title_changed.emit(self.current_title())

    def _on_output(self, text: str) -> None:
        cleaned = strip_ansi(text)
        if not cleaned:
            return
        self._append(cleaned)

    def _append(self, text: str) -> None:
        cursor = self._transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _on_host_key_unknown(self, unknown: object) -> None:
        """Show the fingerprint; connect again if it is the right server."""
        from mysql_runner.ui.host_key_dialog import ask

        if not isinstance(unknown, hostkeys.HostKeyUnknown):
            return
        if self._closing:
            return
        if ask(unknown, self):
            self._say("Server confirmed. Opening a shell…")
            self._open_requested.emit(self._spec, self._start_dir)
            return
        self._on_failed(
            f"No shell: {unknown.host} was not confirmed as your server."
        )

    def _on_failed(self, message: str) -> None:
        if self._closing:
            return
        self._say(message)
        self.status_message.emit(f"{self._profile.label}: {message}")

    def _on_finished(self) -> None:
        self._input.setEnabled(False)
        self._say("The shell has closed.")

    def _say(self, message: str) -> None:
        self._status.setText(message)

    def _on_search_state(self, text: str) -> None:
        if text:
            self._say(text)
        else:
            self._say("")

    # ----- input ----------------------------------------------------------
    def _on_submit(self) -> None:
        if self._input.searching:
            return  # Enter in search mode keeps the match; it does not run it
        text = self._input.text()
        if text.strip() and not self._confirm_destructive(text):
            return
        self._input.clear()
        self._input.reset_walk()
        note = self._history.add(text)
        if note:
            self._say(note)
        self._track_directory(text)
        self._send_requested.emit(text + "\n")

    def _confirm_destructive(self, command: str) -> bool:
        """Ask before something irreversible on a live server. True to go on."""
        if self._profile.environment != Environment.PROD:
            return True
        if not self._settings.production_guard:
            return True
        reason = destructive_reason(command)
        if not reason:
            return True
        answer = QMessageBox.question(
            self,
            "Run this on production?",
            f"{self._profile.label} is marked PRODUCTION, and this command "
            f"would {reason}.\n\n{command}\n\nRun it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _track_directory(self, command: str) -> None:
        """Follow ``cd`` so Tab can complete relative paths.

        The shell's real working directory is not something SSH will tell us
        without asking it, and asking would mean writing into the transcript
        the user did not type. Watching what they typed instead is right for
        every ordinary case and wrong only where a command changed directory
        without being a cd - at which point Tab completes the wrong folder and
        nothing worse happens.
        """
        stripped = command.strip()
        if not stripped.startswith("cd"):
            return
        rest = stripped[2:].strip()
        if not rest or rest.startswith("-"):
            return
        try:
            target = shlex.split(rest)[0]
        except ValueError:
            return
        self._cwd = self._resolve(target)
        self._listings.clear()

    def _resolve(self, path: str) -> str:
        """A remote path made absolute against the directory we think we are in."""
        if path.startswith("/"):
            return posixpath.normpath(path)
        if path.startswith("~"):
            return path  # the shell knows its own home; we do not
        return posixpath.normpath(posixpath.join(self._cwd or "/", path))

    # ----- completion -----------------------------------------------------
    def _on_complete(self, text: str, position: int) -> None:
        token, start = token_at(text, position)
        before = text[:start].strip()
        if not before and "/" not in token and not token.startswith("~"):
            self._complete_command(token, start, position)
            return
        directory, separator, partial = token.rpartition("/")
        base = self._resolve(directory + "/") if separator else (self._cwd or "/")
        if base.startswith("~"):
            self._say("Tab cannot complete inside ~ - type the full path.")
            return
        cached = self._listings.get(base)
        if cached and time.monotonic() - cached[0] < COMPLETION_TTL:
            self._apply_completion(base, cached[1], partial, start)
            return
        self._pending = (base, partial, start)
        self._list_requested.emit(base)

    def _on_listed(self, path: str, names: object) -> None:
        entries = list(names or [])
        self._listings[path] = (time.monotonic(), entries)
        pending, self._pending = self._pending, None
        if pending is None or pending[0] != path:
            return
        _base, partial, start = pending
        self._apply_completion(path, entries, partial, start)

    def _apply_completion(
        self, base: str, entries: list, partial: str, start: int
    ) -> None:
        matches = [
            (name + "/" if is_dir else name)
            for name, is_dir in entries
            if name.startswith(partial)
        ]
        if not matches:
            self._say(f"Nothing in {base} starts with {partial!r}.")
            return
        if len(matches) == 1:
            self._fill(start, partial, matches[0], finished=True)
            return
        shared = common_prefix(matches)
        if len(shared) > len(partial):
            self._fill(start, partial, shared, finished=False)
            return
        # Nothing more to fill in: show what the choices are, the way a shell
        # does, rather than silently doing nothing and looking broken.
        shown = matches[:COMPLETION_LIST_LIMIT]
        self._append("\n" + "   ".join(shown) + "\n")
        if len(matches) > len(shown):
            self._append(f"… and {len(matches) - len(shown)} more\n")
        self._say(f"{len(matches)} matches in {base}.")

    def _fill(self, start: int, partial: str, value: str, *, finished: bool) -> None:
        """Replace the token being completed with ``value``."""
        text = self._input.text()
        end = start + len(partial)
        tail = " " if finished and not value.endswith("/") else ""
        replaced = text[:start] + value + tail + text[end:]
        self._input.setText(replaced)
        self._input.setCursorPosition(start + len(value) + len(tail))
        self._input.reset_walk()
        self._say("")

    def _complete_command(self, token: str, start: int, position: int) -> None:
        """Complete the first word from what has been run here, then the usual."""
        used = self._history.commands()
        pool = used + [c for c in _COMMON_COMMANDS if c not in used]
        matches = [name for name in pool if name.startswith(token)]
        if not matches:
            return
        if len(matches) == 1:
            self._fill(start, token, matches[0], finished=True)
            return
        shared = common_prefix(matches)
        if len(shared) > len(token):
            self._fill(start, token, shared, finished=False)
            return
        self._say("   ".join(matches[:40]))

    # ----- teardown -------------------------------------------------------
    def cleanup(self) -> None:
        self._closing = True
        try:
            self._worker.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self._close_requested.emit()
        except RuntimeError:
            pass
        self._thread.quit()
        self._thread.wait(3000)
