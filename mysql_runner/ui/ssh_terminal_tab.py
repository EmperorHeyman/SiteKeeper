"""An embedded SSH shell, opened in the directory you were looking at.

This is a line-oriented console, not a full terminal emulator: you type a
command, it goes to the shell, and the output appears. That covers what a file
manager's shell is actually for - a quick ``composer install``, a ``git pull``,
a look at a config - without pretending to be a replacement for a real terminal
(there is a button for that too, see ``transfer/spawn.py``).

The shell runs on its own SSH connection so nothing here can stall the file
panes, and colour escape sequences are stripped so the transcript stays
readable and copyable.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage.models import Environment, ServerProfile
from mysql_runner.transfer.base import TransferError
from mysql_runner.transfer.worker import ConnectionSpec
from mysql_runner.ui import theme

#: How often to look for new output, in milliseconds.
POLL_MS = 80

#: Keep the transcript bounded so a runaway command cannot exhaust memory.
MAX_BLOCKS = 20_000

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]|[\x00\x07\x0f]")



def strip_ansi(text: str) -> str:
    """Remove colour and cursor escapes, and normalise line endings."""
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


class _ShellWorker(QObject):
    """Owns the SSH channel and does the reading, off the GUI thread."""

    opened = pyqtSignal(str)
    output = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

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
            self.send(f"cd {_quote(cwd)}\n")
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
    """Input line that walks the history with the arrow keys."""

    history_back = pyqtSignal()
    history_forward = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Up:
            self.history_back.emit()
            return
        if event.key() == Qt.Key.Key_Down:
            self.history_forward.emit()
            return
        super().keyPressEvent(event)


class SshTerminalTab(QWidget):
    """A shell session on the server, as a tab."""

    status_message = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    _open_requested = pyqtSignal(object, str)
    _send_requested = pyqtSignal(str)
    _close_requested = pyqtSignal()

    def __init__(
        self,
        profile: ServerProfile,
        spec: ConnectionSpec,
        cwd: str = "",
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._cwd = cwd
        self._history: list[str] = []
        self._index = 0
        self._closing = False

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
        self._transcript.setFont(_mono_font())
        self._transcript.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._transcript, 1)

        row = QHBoxLayout()
        if self._profile.environment == Environment.PROD:
            # Beside the command line, where it is in view while you type.
            row.addWidget(
                theme.production_badge("This shell runs on the live server.")
            )
        self._input = _CommandLine()
        self._input.setFont(_mono_font())
        self._input.setPlaceholderText("Connecting…")
        self._input.setEnabled(False)
        self._input.returnPressed.connect(self._on_submit)
        self._input.history_back.connect(lambda: self._walk(-1))
        self._input.history_forward.connect(lambda: self._walk(1))
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
        self._close_requested.connect(self._worker.close_shell)
        self._worker.opened.connect(self._on_opened)
        self._worker.output.connect(self._on_output)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_opened(self, banner: str) -> None:
        self._status.setText(banner)
        self._input.setEnabled(True)
        self._input.setPlaceholderText("Type a command and press Enter")
        self._input.setFocus()
        self.status_message.emit(banner)
        self.title_changed.emit(self.current_title())

    def _on_output(self, text: str) -> None:
        cleaned = strip_ansi(text)
        if not cleaned:
            return
        cursor = self._transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(cleaned)
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _on_failed(self, message: str) -> None:
        if self._closing:
            return
        self._status.setText(message)
        self.status_message.emit(f"{self._profile.label}: {message}")

    def _on_finished(self) -> None:
        self._input.setEnabled(False)
        self._status.setText("The shell has closed.")

    # ----- input ----------------------------------------------------------
    def _on_submit(self) -> None:
        text = self._input.text()
        self._input.clear()
        if text.strip():
            self._history.append(text)
        self._index = len(self._history)
        self._send_requested.emit(text + "\n")

    def _walk(self, delta: int) -> None:
        if not self._history:
            return
        self._index = max(0, min(len(self._history), self._index + delta))
        self._input.setText(
            self._history[self._index] if self._index < len(self._history) else ""
        )

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


def _quote(path: str) -> str:
    import shlex

    return shlex.quote(path)


def _mono_font() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font
