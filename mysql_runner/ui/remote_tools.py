"""Dialogs for the server-side tools: search, disk usage, archives, commands.

Each one is a thin shell: it collects what the user wants, hands it to the
worker thread, and renders whatever comes back. None of them talk to the
network themselves, which is why none of them can freeze the window.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.transfer.remote_exec import ARCHIVE_KINDS, TAR_GZ
from mysql_runner.transfer.snippets import (
    PLACEHOLDERS,
    Snippet,
    SnippetLibrary,
    missing_placeholders,
    render,
)


#: Object name that the application stylesheet renders as a grey note.
HINT_ROLE = "hint"


def mono_font() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font


def human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# ----- search -------------------------------------------------------------
class RemoteSearchDialog(QDialog):
    """Search file contents on the server, and jump to what it finds."""

    search_requested = pyqtSignal(str, str, bool, bool, str)  # root, pattern, fixed, icase, include
    open_requested = pyqtSignal(str, int)  # path, line

    def __init__(self, root: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Search on the server")
        self.setModal(False)
        self.resize(760, 480)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._root = QLineEdit(root)
        self._pattern = QLineEdit()
        self._pattern.setPlaceholderText("Text to find")
        self._pattern.returnPressed.connect(self._start)
        self._include = QLineEdit()
        self._include.setPlaceholderText("*.php  (optional)")
        form.addRow("Search in:", self._root)
        form.addRow("For:", self._pattern)
        form.addRow("Only files matching:", self._include)
        layout.addLayout(form)

        options = QHBoxLayout()
        self._regex = QCheckBox("Regular expression")
        self._icase = QCheckBox("Ignore case")
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._start)
        options.addWidget(self._regex)
        options.addWidget(self._icase)
        options.addStretch(1)
        options.addWidget(self._search_btn)
        layout.addLayout(options)

        self._results = QTreeWidget()
        self._results.setColumnCount(3)
        self._results.setHeaderLabels(["File", "Line", "Match"])
        self._results.setRootIsDecorated(False)
        self._results.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._results.itemDoubleClicked.connect(self._on_open)
        layout.addWidget(self._results, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def _start(self) -> None:
        pattern = self._pattern.text()
        if not pattern.strip():
            self._status.setText("Type something to search for.")
            return
        self._results.clear()
        self._status.setText("Searching…")
        self._search_btn.setEnabled(False)
        self.search_requested.emit(
            self._root.text().strip() or "/",
            pattern,
            not self._regex.isChecked(),
            self._icase.isChecked(),
            self._include.text().strip(),
        )

    def show_results(self, result) -> None:
        self._search_btn.setEnabled(True)
        self._results.clear()
        for hit in result.hits:
            row = QTreeWidgetItem(self._results)
            row.setText(0, hit.path)
            row.setText(1, str(hit.line))
            row.setText(2, hit.text.strip()[:300])
            row.setData(0, Qt.ItemDataRole.UserRole, (hit.path, hit.line))
        if result.error:
            self._status.setText(result.error)
            return
        text = f"{len(result.hits)} match(es) via {result.tool or 'grep'}"
        if result.truncated:
            text += " — more were found than are shown"
        self._status.setText(text)

    def show_error(self, message: str) -> None:
        self._search_btn.setEnabled(True)
        self._status.setText(message)

    def _on_open(self, row: QTreeWidgetItem) -> None:
        data = row.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.open_requested.emit(data[0], data[1])


# ----- disk usage ---------------------------------------------------------
class DiskUsageDialog(QDialog):
    """Where the space went, as bars you can walk into."""

    usage_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Disk usage")
        self.setModal(False)
        self.resize(680, 460)
        self._path = path

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._path_label = QLabel(path)
        self._path_label.setStyleSheet("font-weight: bold;")
        up = QPushButton("Up")
        up.clicked.connect(self._go_up)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.usage_requested.emit(self._path))
        header.addWidget(self._path_label, 1)
        header.addWidget(up)
        header.addWidget(refresh)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Name", "Size", "Share"])
        self._tree.setRootIsDecorated(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.itemDoubleClicked.connect(self._on_enter)
        layout.addWidget(self._tree, 1)

        self._total = QLabel("Measuring…")
        layout.addWidget(self._total)

        row = QHBoxLayout()
        open_btn = QPushButton("Show in the file list")
        open_btn.clicked.connect(self._on_open)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(open_btn)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)

    def start(self) -> None:
        self.usage_requested.emit(self._path)

    def show_usage(self, usage) -> None:
        self._path = usage.root
        self._path_label.setText(usage.root)
        self._tree.clear()
        for entry in usage.entries:
            row = QTreeWidgetItem(self._tree)
            row.setText(0, entry.name)
            row.setText(1, human_size(entry.size))
            share = usage.share(entry)
            row.setText(2, _bar(share))
            row.setData(0, Qt.ItemDataRole.UserRole, entry.path)
            self._tree.addTopLevelItem(row)
        self._total.setText(f"{human_size(usage.total)} in total under {usage.root}")

    def show_error(self, message: str) -> None:
        self._total.setText(message)

    def _selected_path(self) -> str:
        rows = self._tree.selectedItems()
        if not rows:
            return ""
        return str(rows[0].data(0, Qt.ItemDataRole.UserRole) or "")

    def _on_enter(self, row: QTreeWidgetItem) -> None:
        path = str(row.data(0, Qt.ItemDataRole.UserRole) or "")
        if path:
            self._path = path
            self.usage_requested.emit(path)

    def _go_up(self) -> None:
        parent = self._path.rstrip("/").rsplit("/", 1)[0] or "/"
        self._path = parent
        self.usage_requested.emit(parent)

    def _on_open(self) -> None:
        path = self._selected_path() or self._path
        if path:
            self.open_requested.emit(path)


def _bar(share: float, width: int = 24) -> str:
    """A text bar - no pixel art, and it copies and pastes."""
    filled = max(0, min(width, round(share * width)))
    return "█" * filled + "·" * (width - filled) + f"  {share * 100:4.1f}%"


# ----- archives -----------------------------------------------------------
class ArchiveDialog(QDialog):
    """Name the archive and pick its format."""

    def __init__(self, count: int, suggested: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create an archive on the server")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(f"Pack {count} selected item(s) into an archive, server-side.")
        )
        form = QFormLayout()
        self._kind = QComboBox()
        for key in ARCHIVE_KINDS:
            self._kind.addItem(key, key)
        index = self._kind.findData(TAR_GZ)
        if index >= 0:
            self._kind.setCurrentIndex(index)
        self._kind.currentIndexChanged.connect(self._sync_name)
        self._name = QLineEdit(suggested)
        form.addRow("Format:", self._kind)
        form.addRow("File name:", self._name)
        layout.addLayout(form)
        note = QLabel(
            "The archive is built by the server, so nothing is downloaded or "
            "uploaded to make it."
        )
        note.setWordWrap(True)
        note.setObjectName(HINT_ROLE)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_name()

    def _sync_name(self) -> None:
        stem = self._name.text()
        for suffix in ARCHIVE_KINDS.values():
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        self._name.setText(stem + ARCHIVE_KINDS[self.kind()])

    def kind(self) -> str:
        return str(self._kind.currentData())

    def name(self) -> str:
        return self._name.text().strip()


# ----- one-off commands ---------------------------------------------------
class CommandBar(QDialog):
    """A slim prompt for a single command, with its output underneath."""

    command_requested = pyqtSignal(str, str)  # command, cwd

    def __init__(self, cwd: str, *, history: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run a command on the server")
        self.setModal(False)
        self.resize(720, 380)
        self._cwd = cwd
        self._history = list(history or [])
        self._index = len(self._history)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Working directory: {cwd}"))
        self._input = QLineEdit()
        self._input.setFont(mono_font())
        self._input.setPlaceholderText("systemctl restart nginx")
        self._input.returnPressed.connect(self._run)
        layout.addWidget(self._input)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(mono_font())
        layout.addWidget(self._output, 1)

        row = QHBoxLayout()
        run = QPushButton("Run")
        run.clicked.connect(self._run)
        row.addStretch(1)
        row.addWidget(run)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._input.hasFocus() and event.key() in (
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            self._walk_history(-1 if event.key() == Qt.Key.Key_Up else 1)
            return
        super().keyPressEvent(event)

    def _walk_history(self, delta: int) -> None:
        if not self._history:
            return
        self._index = max(0, min(len(self._history), self._index + delta))
        self._input.setText(
            self._history[self._index] if self._index < len(self._history) else ""
        )

    def _run(self) -> None:
        command = self._input.text().strip()
        if not command:
            return
        self._history.append(command)
        self._index = len(self._history)
        self._output.appendPlainText(f"$ {command}")
        self._input.clear()
        self.command_requested.emit(command, self._cwd)

    def show_result(self, result) -> None:
        if result.stdout:
            self._output.appendPlainText(result.stdout.rstrip())
        if result.stderr:
            self._output.appendPlainText(result.stderr.rstrip())
        if not result.ok:
            self._output.appendPlainText(f"[exit status {result.exit_status}]")
        self._output.appendPlainText("")

    def show_error(self, message: str) -> None:
        self._output.appendPlainText(f"[{message}]\n")

    def history(self) -> list[str]:
        return list(self._history)

    def prefill(self, command: str) -> None:
        self._input.setText(command)
        self._input.setFocus()


# ----- snippets -----------------------------------------------------------
class SnippetsDialog(QDialog):
    """The snippet drawer: pick one to run, or edit the library."""

    run_requested = pyqtSignal(str)  # the rendered command

    def __init__(
        self,
        library: SnippetLibrary,
        context: dict[str, str],
        *,
        can_run: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Snippets")
        self.setModal(False)
        self.resize(760, 460)
        self._library = library
        self._context = context
        self._current: Snippet | None = None

        layout = QVBoxLayout(self)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter…")
        self._filter.textChanged.connect(self._refresh)
        layout.addWidget(self._filter)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_select)
        splitter.addWidget(self._list)

        editor = QWidget()
        form = QFormLayout(editor)
        self._name = QLineEdit()
        self._command = QPlainTextEdit()
        self._command.setFont(mono_font())
        self._command.setMaximumHeight(110)
        self._command.textChanged.connect(self._sync_preview)
        self._description = QLineEdit()
        self._tags = QLineEdit()
        self._confirm = QCheckBox("Ask before running")
        form.addRow("Name:", self._name)
        form.addRow("Command:", self._command)
        form.addRow("Description:", self._description)
        form.addRow("Tags:", self._tags)
        form.addRow(self._confirm)
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setFont(mono_font())
        self._preview.setObjectName(HINT_ROLE)
        form.addRow("Will run:", self._preview)
        hint = QLabel(
            "Placeholders: " + ", ".join(f"{{{name}}}" for name, _ in PLACEHOLDERS)
        )
        hint.setWordWrap(True)
        hint.setObjectName(HINT_ROLE)
        form.addRow(hint)
        splitter.addWidget(editor)
        splitter.setSizes([240, 500])
        layout.addWidget(splitter, 1)

        row = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._on_new)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_delete)
        self._run_btn = QPushButton("Run on the server")
        self._run_btn.setEnabled(can_run)
        self._run_btn.setToolTip(
            "" if can_run else "This connection has no shell, so nothing can be run."
        )
        self._run_btn.clicked.connect(self._on_run)
        row.addWidget(new_btn)
        row.addWidget(save_btn)
        row.addWidget(delete_btn)
        row.addStretch(1)
        row.addWidget(self._run_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._refresh()

    # ----- list -----------------------------------------------------------
    def _refresh(self) -> None:
        self._list.clear()
        for snippet in self._library.search(self._filter.text()):
            item = QListWidgetItem(snippet.name)
            item.setData(Qt.ItemDataRole.UserRole, snippet.id)
            item.setToolTip(snippet.description or snippet.command)
            self._list.addItem(item)

    def _on_select(self, current: QListWidgetItem | None) -> None:
        if current is None:
            return
        snippet = self._library.get(str(current.data(Qt.ItemDataRole.UserRole)))
        if snippet is None:
            return
        self._current = snippet
        self._name.setText(snippet.name)
        self._command.setPlainText(snippet.command)
        self._description.setText(snippet.description)
        self._tags.setText(", ".join(snippet.tags))
        self._confirm.setChecked(snippet.confirm)
        self._sync_preview()

    def _sync_preview(self) -> None:
        command = self._command.toPlainText().strip()
        self._preview.setText(render(command, self._context) if command else "")

    # ----- editing --------------------------------------------------------
    def _collect(self) -> Snippet:
        tags = [tag.strip() for tag in self._tags.text().split(",") if tag.strip()]
        snippet = Snippet(
            name=self._name.text().strip() or "(unnamed)",
            command=self._command.toPlainText().strip(),
            description=self._description.text().strip(),
            confirm=self._confirm.isChecked(),
            tags=tags,
        )
        if self._current is not None:
            snippet.id = self._current.id
        return snippet

    def _on_new(self) -> None:
        self._current = None
        self._name.clear()
        self._command.clear()
        self._description.clear()
        self._tags.clear()
        self._confirm.setChecked(False)
        self._name.setFocus()

    def _on_save(self) -> None:
        snippet = self._collect()
        if not snippet.command:
            QMessageBox.information(self, "Nothing to save", "Type a command first.")
            return
        if self._current is None:
            self._library.add(snippet)
        else:
            self._library.update(snippet)
        self._current = snippet
        self._refresh()
        self._sync_preview()

    def _on_delete(self) -> None:
        if self._current is None:
            return
        if self._library.delete(self._current.id):
            self._current = None
            self._on_new()
            self._refresh()

    def _on_run(self) -> None:
        command = self._command.toPlainText().strip()
        if not command:
            return
        missing = missing_placeholders(command, self._context)
        if missing:
            QMessageBox.information(
                self,
                "Not enough context",
                "This snippet needs " + ", ".join(missing) + ", which this tab "
                "cannot supply. Select a file first, or edit the snippet.",
            )
            return
        rendered = render(command, self._context)
        if self._confirm.isChecked():
            confirm = QMessageBox.question(
                self, "Run this?", f"Run on the server:\n\n{rendered}"
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self.run_requested.emit(rendered)


# ----- symlinks -----------------------------------------------------------
class LinkTargetDialog(QDialog):
    """Read or retarget a symlink."""

    def __init__(self, name: str, target: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Link target — {name}")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{name} currently points at:"))
        self._target = QLineEdit(target)
        self._target.setFont(mono_font())
        self._target.setMinimumWidth(420)
        layout.addWidget(self._target)
        note = QLabel(
            "Changing this replaces the link. On a release layout that is how "
            "you switch which version is live."
        )
        note.setWordWrap(True)
        note.setObjectName(HINT_ROLE)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def target(self) -> str:
        return self._target.text().strip()


# ----- progress for long tool jobs ---------------------------------------
class ToolProgressBar(QWidget):
    """An indeterminate bar with a label and a cancel button."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setMaximumWidth(160)
        self._label = QLabel("")
        cancel = QPushButton("Stop")
        cancel.clicked.connect(self.cancel_requested.emit)
        layout.addWidget(self._bar)
        layout.addWidget(self._label, 1)
        layout.addWidget(cancel)
        self.setVisible(False)

    def start(self, text: str) -> None:
        self._label.setText(text)
        self.setVisible(True)

    def update_text(self, text: str) -> None:
        self._label.setText(text)

    def stop(self) -> None:
        self.setVisible(False)
        self._label.setText("")
