"""Live remote log viewer - ``tail -f`` without downloading the log.

Error logs on a busy host are gigabytes; the interesting part is the last
twenty lines and whatever arrives next. This streams exactly that over the SSH
connection, with a filter for when the noise gets in the way.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from mysql_runner.transfer.base import TransferError
from mysql_runner.transfer.worker import ConnectionSpec
from mysql_runner.ui.ssh_terminal_tab import strip_ansi

#: How often to poll the stream, in milliseconds.
POLL_MS = 150

#: Keep the view bounded; a chatty log would otherwise fill memory.
MAX_BLOCKS = 50_000

#: How many lines of history to fetch when the stream opens.
INITIAL_LINES = 200


class _TailWorker(QObject):
    """Runs ``tail -f`` on its own connection and relays what it prints."""

    opened = pyqtSignal(str)
    output = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._fs = None
        self._stream = None
        self._timer: QTimer | None = None

    @pyqtSlot(object, str, int)
    def start(self, spec: object, path: str, lines: int) -> None:
        assert isinstance(spec, ConnectionSpec)
        self.stop()
        try:
            fs = spec.build()
            fs.connect()
            from mysql_runner.transfer.remote_exec import tail

            stream = tail(fs, path, lines=lines, follow=True)
        except TransferError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self._fs = fs
        self._stream = stream
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._drain)
        self._timer.start()
        self.opened.emit(path)

    @pyqtSlot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._fs is not None:
            try:
                self._fs.close()
            except Exception:
                pass
            self._fs = None
        self.finished.emit()

    def _drain(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            text = stream.read_text(0.0)
        except Exception as exc:
            self.failed.emit(str(exc))
            self.stop()
            return
        if text:
            self.output.emit(text)
        elif not stream.active():
            self.output.emit("\n[the log stream ended]\n")
            self.stop()


class LogViewerDialog(QDialog):
    """A window that follows one log file."""

    def __init__(
        self,
        spec: ConnectionSpec,
        path: str,
        *,
        candidates: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Live log")
        self.setModal(False)
        self.resize(900, 520)
        self._spec = spec
        self._paused = False
        self._buffer: list[str] = []

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._path = QComboBox()
        self._path.setEditable(True)
        for candidate in candidates or []:
            self._path.addItem(candidate)
        if path:
            self._path.insertItem(0, path)
            self._path.setCurrentIndex(0)
        follow = QPushButton("Follow")
        follow.clicked.connect(self._restart)
        top.addWidget(QLabel("Log file:"))
        top.addWidget(self._path, 1)
        top.addWidget(follow)
        layout.addLayout(top)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(MAX_BLOCKS)
        self._view.setFont(_mono_font())
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._view, 1)

        controls = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Only show lines containing…")
        self._filter.textChanged.connect(self._reapply_filter)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._wrap = QCheckBox("Wrap")
        self._wrap.toggled.connect(self._toggle_wrap)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        save = QPushButton("Save…")
        save.clicked.connect(self._save)
        controls.addWidget(self._filter, 1)
        controls.addWidget(self._wrap)
        controls.addWidget(self._pause_btn)
        controls.addWidget(clear)
        controls.addWidget(save)
        layout.addLayout(controls)

        self._status = QLabel("Connecting…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._start_worker()
        if path:
            self._restart()

    # ----- worker ---------------------------------------------------------
    _start_requested = pyqtSignal(object, str, int)
    _stop_requested = pyqtSignal()

    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = _TailWorker()
        self._worker.moveToThread(self._thread)
        self._start_requested.connect(self._worker.start)
        self._stop_requested.connect(self._worker.stop)
        self._worker.opened.connect(
            lambda path: self._status.setText(f"Following {path}")
        )
        self._worker.output.connect(self._on_output)
        self._worker.failed.connect(self._status.setText)
        self._thread.start()

    def _restart(self) -> None:
        path = self._path.currentText().strip()
        if not path:
            self._status.setText("Give the path of a log file.")
            return
        self._view.clear()
        self._buffer.clear()
        self._status.setText(f"Opening {path}…")
        self._start_requested.emit(self._spec, path, INITIAL_LINES)

    # ----- content --------------------------------------------------------
    def _on_output(self, text: str) -> None:
        cleaned = strip_ansi(text)
        if not cleaned:
            return
        for line in cleaned.splitlines():
            self._buffer.append(line)
        if len(self._buffer) > MAX_BLOCKS:
            del self._buffer[: len(self._buffer) - MAX_BLOCKS]
        if self._paused:
            return
        needle = self._filter.text().strip()
        for line in cleaned.splitlines():
            if not needle or needle.lower() in line.lower():
                self._view.appendPlainText(line)

    def _reapply_filter(self) -> None:
        needle = self._filter.text().strip().lower()
        self._view.clear()
        for line in self._buffer:
            if not needle or needle in line.lower():
                self._view.appendPlainText(line)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause_btn.setText("Resume" if self._paused else "Pause")
        if not self._paused:
            self._reapply_filter()

    def _toggle_wrap(self, wrap: bool) -> None:
        self._view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrap
            else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def _clear(self) -> None:
        self._buffer.clear()
        self._view.clear()

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save what is shown", "log.txt", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(self._buffer))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        self._status.setText(f"Saved to {path}")

    # ----- teardown -------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        try:
            self._worker.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self._stop_requested.emit()
        except RuntimeError:
            pass
        self._thread.quit()
        self._thread.wait(3000)
        super().closeEvent(event)


def _mono_font() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font
