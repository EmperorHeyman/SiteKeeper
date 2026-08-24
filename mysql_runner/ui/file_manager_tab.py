"""Dual-pane FTP/FTPS/SFTP file manager tab.

Local files on the left, the remote server on the right, and a transfer bar in
between - the WinSCP layout. Both panes are the same widget class fed from
different sources: the local one reads the disk directly, the remote one is
filled by the transfer worker thread so the window never blocks on the network.

Beyond copying files, the tab is the front end for everything in
``mysql_runner/transfer``: per-pane navigation history with a real Back button,
folder sizes and dates that account for their contents, hash comparison
between the two sides, an ignore engine, a controllable multi-connection
transfer queue, shadow backups with undo, mirrored navigation, permissions,
symlinks, and - on SFTP - server-side archives, search, disk usage, a shell and
live logs.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime

from PyQt6.QtCore import QMimeData, QSize, QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDrag, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage.models import Environment, ServerProfile
from mysql_runner.storage.settings import Settings
from mysql_runner.transfer import permissions as perm
from mysql_runner.transfer import spawn
from mysql_runner.transfer.base import Capability, RemoteEntry, RemoteFS
from mysql_runner.transfer.gitwatch import (
    CommitEvent,
    GitCommitWatcher,
    describe_repo,
)
from mysql_runner.transfer.hashing import DiffStatus
from mysql_runner.transfer.history import HistoryStore
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.navhistory import NavHistory, mirror_path
from mysql_runner.transfer.pool import Overwrite, PoolOptions
from mysql_runner.transfer.snippets import SnippetLibrary
from mysql_runner.transfer.syncrules import SyncMode, SyncRule, SyncRuleStore
from mysql_runner.transfer.treestat import FolderStatsCache, local_folder_stats
from mysql_runner.transfer.watcher import Change, ChangeKind, DirectoryWatcher, summarise
from mysql_runner.transfer.worker import ConnectionSpec, TransferWorker
from mysql_runner.ui import theme
from mysql_runner.ui.compare_dialog import CompareDialog
from mysql_runner.ui.history_dialog import HistoryDialog
from mysql_runner.ui.log_viewer import LogViewerDialog
from mysql_runner.ui.permissions_dialog import PermissionsDialog
from mysql_runner.ui.sync_dialog import SyncFoldersDialog
from mysql_runner.ui.remote_tools import (
    ArchiveDialog,
    CommandBar,
    DiskUsageDialog,
    LinkTargetDialog,
    RemoteSearchDialog,
    SnippetsDialog,
    ToolProgressBar,
)
from mysql_runner.ui.transfer_queue_panel import TransferQueuePanel

_PARENT = ".."
_COLUMNS = ("Name", "Size", "Modified", "Mode", "Sync")
_NAME, _SIZE, _MODIFIED, _MODE, _SYNC = range(5)

#: Marker per comparison verdict; the colour comes from the theme.
_DIFF_MARK = {
    DiffStatus.SAME: "=",
    DiffStatus.DIFFERENT: "≠",
    DiffStatus.LOCAL_ONLY: "→",
    DiffStatus.REMOTE_ONLY: "←",
    DiffStatus.UNKNOWN: "?",
}

#: Don't try to measure every subfolder of a directory with hundreds of them.
_MAX_STAT_FOLDERS = 120

#: Rows dragged from one pane to the other travel under this MIME type. Local
#: rows also carry text/uri-list, so they can be dropped on Explorer or an
#: editor as well; remote rows have no real files to offer, so they cannot.
_ROWS_MIME = "application/x-mysqlrunner-rows"

#: Mark on a folder that keeps itself on the server.
_SYNC_MARK = "⟳"

#: Deleting more than this many server files in one automatic sync is treated
#: as a mistake worth asking about, even when removals were approved once.
_BULK_REMOVAL = 25

#: How close to a pane's top or bottom edge a drag has to be before the
#: listing starts scrolling itself, and how often it steps while it is there.
_DRAG_EDGE = 32
_DRAG_SCROLL_MS = 60


class _FileTable(QTableWidget):
    """Listing table that reports when it takes focus, and starts drags."""

    focused = pyqtSignal()

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        # The pane installs this: it knows which side it is and what is picked.
        self.drag_payload = lambda: None

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.focused.emit()
        super().focusInEvent(event)

    def startDrag(self, _actions) -> None:  # noqa: N802 - Qt naming
        """Begin dragging the selected rows out of this pane."""
        data = self.drag_payload()
        if data is None:
            return
        drag = QDrag(self)
        drag.setMimeData(data)
        drag.exec(Qt.DropAction.CopyAction)


class _FilePane(QWidget):
    """A path bar plus a listing table, used for both the local and remote side."""

    #: The user asked to show a different directory.
    navigate = pyqtSignal(str)
    #: The table gained focus (so it is the pane commands act on).
    focused = pyqtSignal()
    #: A right-click, with the global position for the menu.
    menu_requested = pyqtSignal(object)
    #: Local paths dropped on this pane from outside the app:
    #: {"paths": [...], "target": directory}.
    paths_dropped = pyqtSignal(object)
    #: Rows dragged in from the other pane:
    #: {"remote": bool, "base": str, "items": [[name, is_dir]], "target": str}.
    transfer_dropped = pyqtSignal(object)

    def __init__(self, title: str, parent: QWidget | None = None, *, posix: bool = False) -> None:
        super().__init__(parent)
        self._path = ""
        self._entries: list[RemoteEntry] = []
        self._posix = posix
        self._history = NavHistory()
        self._replaying = False
        self._diff_colours = theme.diff_colours(False)
        #: name -> tooltip for folders that keep themselves on the server.
        self._sync_marks: dict[str, str] = {}
        #: Row a drop would land in, tinted while a drag hovers over it.
        self._hover_row = -1
        self._hover_colour = _hover_colour(False)
        #: Held drag near an edge scrolls the listing: -1 up, +1 down, 0 still.
        self._scroll_step = 0
        self._drag_point = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(_DRAG_SCROLL_MS)
        self._scroll_timer.timeout.connect(self._auto_scroll)
        # Replaced by bind_paths(); the defaults keep the pane inert until then.
        self._child_path = lambda name: name
        self._parent_path = lambda: self._path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self._title = QLabel(title)
        self._title.setObjectName("title")
        self._back = _icon_button("back", "Back (Alt+Left)")
        self._back.clicked.connect(self.go_back)
        self._forward = _icon_button("forward", "Forward (Alt+Right)")
        self._forward.clicked.connect(self.go_forward)
        self._recent = _icon_button("down", "Recently visited")
        self._recent.clicked.connect(self._show_recent)
        self._up = _icon_button("up", "Parent directory (Alt+Up)")
        self._up.clicked.connect(self._go_up)
        self._refresh_btn = _icon_button("refresh", "Refresh (F5)")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._title)
        header.addStretch(1)
        self._nav_buttons = {
            "back": self._back,
            "forward": self._forward,
            "down": self._recent,
            "up": self._up,
            "refresh": self._refresh_btn,
        }
        for button in self._nav_buttons.values():
            header.addWidget(button)
        layout.addLayout(header)

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Path")
        self._path_edit.returnPressed.connect(
            lambda: self.navigate.emit(self._path_edit.text().strip())
        )
        layout.addWidget(self._path_edit)

        self._table = _FileTable(0, len(_COLUMNS))
        self._table.focused.connect(self.focused.emit)
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(22)
        header = self._table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setHighlightSections(False)
        self._table.horizontalHeader().setSectionResizeMode(
            _NAME, QHeaderView.ResizeMode.Stretch
        )
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_menu)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        self._table.setAcceptDrops(True)
        self._table.setDragEnabled(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._table.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._table.drag_payload = self._drag_payload
        self._table.dragEnterEvent = self._on_drag  # type: ignore[method-assign]
        self._table.dragMoveEvent = self._on_drag  # type: ignore[method-assign]
        self._table.dragLeaveEvent = self._on_drag_leave  # type: ignore[method-assign]
        self._table.dropEvent = self._on_drop  # type: ignore[method-assign]
        layout.addWidget(self._table, 1)

        self._sync_buttons()

    # ----- content --------------------------------------------------------
    def set_listing(self, path: str, entries: list[RemoteEntry], *, at_root: bool) -> None:
        self._path = path
        self._entries = list(entries)
        self._path_edit.setText(path)
        if not self._replaying:
            self._history.visit(path)
        self._sync_buttons()
        self._render()

    def _render(self) -> None:
        table = self._table
        table.setRowCount(0)
        self._hover_row = -1
        if not self._at_root():
            self._append_row(_PARENT, "", "", "", is_parent=True)
        for entry in self._entries:
            self._append_row(
                _display_name(entry),
                "" if entry.is_dir and not entry.size else _human_size(entry.size),
                _human_time(entry.modified),
                perm.to_octal(entry.mode) if entry.mode is not None else "",
                is_dir=entry.is_dir,
                name=entry.name,
                is_link=entry.is_link,
                sync_hint=self._sync_marks.get(entry.name, ""),
            )
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(
            _NAME, QHeaderView.ResizeMode.Stretch
        )

    def _at_root(self) -> bool:
        if not self._path:
            return True
        if self._posix:
            return self._path in ("/", "")
        return os.path.dirname(self._path.rstrip("\\/")) in ("", self._path)

    def _append_row(  # noqa: PLR0913 - one row, one argument each
        self,
        label: str,
        size: str,
        modified: str,
        mode: str,
        *,
        is_dir: bool = False,
        is_parent: bool = False,
        name: str = "",
        is_link: bool = False,
        sync_hint: str = "",
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        first = QTableWidgetItem(f"{_SYNC_MARK} {label}" if sync_hint else label)
        first.setData(
            Qt.ItemDataRole.UserRole,
            (name or label, is_dir or is_parent, is_parent),
        )
        if is_parent:
            first.setToolTip("Go up one directory")
        elif sync_hint:
            first.setToolTip(sync_hint)
        if is_link:
            font = first.font()
            font.setItalic(True)
            first.setFont(font)
        self._table.setItem(row, _NAME, first)
        size_item = QTableWidgetItem(size)
        size_item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._table.setItem(row, _SIZE, size_item)
        self._table.setItem(row, _MODIFIED, QTableWidgetItem(modified))
        self._table.setItem(row, _MODE, QTableWidgetItem(mode))
        self._table.setItem(row, _SYNC, QTableWidgetItem(""))

    def update_entries(self, entries: list[RemoteEntry]) -> None:
        """Replace the rows in place (used when folder statistics arrive).

        Whatever was selected stays selected: statistics landing a second later
        must not throw away the selection the user just made.
        """
        chosen = {name for name, _ in self.selection()}
        self._entries = list(entries)
        self._render()
        if chosen:
            self._reselect(chosen)

    def _reselect(self, names: set[str]) -> None:
        self._table.clearSelection()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _NAME)
            if item is None:
                continue
            name, _is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
            if not is_parent and name in names:
                self._table.selectRow(row)

    @property
    def entries(self) -> list[RemoteEntry]:
        return list(self._entries)

    def set_diff(self, report, base_rel_of) -> None:
        """Mark each row with how it compares to the other side."""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _NAME)
            sync = self._table.item(row, _SYNC)
            if item is None or sync is None:
                continue
            name, is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
            if is_parent:
                continue
            status = None
            if report is not None:
                rel = base_rel_of(name)
                if rel is not None:
                    status = report.status_of_name(
                        os.path.dirname(rel).replace("\\", "/"),
                        os.path.basename(rel),
                        is_dir=is_dir,
                    )
            sync.setText(_DIFF_MARK.get(status, ""))
            colour = self._diff_colours.get(status.value) if status is not None else ""
            if colour:
                sync.setForeground(QColor(colour))

    def set_diff_colours(self, colours: dict[str, str]) -> None:
        """Follow the application theme when marking comparison verdicts."""
        self._diff_colours = colours

    def set_sync_marks(self, marks: dict[str, str]) -> None:
        """Mark folders kept on the server: entry name -> what to say about it."""
        if marks == self._sync_marks:
            return
        self._sync_marks = dict(marks)
        self._render()

    def set_theme(self, dark: bool) -> None:
        """Repaint the navigation glyphs for the current theme."""
        for kind, button in self._nav_buttons.items():
            button.setIcon(theme.nav_icon(kind, dark))
        self._hover_colour = _hover_colour(dark)

    def clear_diff(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _SYNC)
            if item is not None:
                item.setText("")

    # ----- accessors ------------------------------------------------------
    @property
    def path(self) -> str:
        return self._path

    @property
    def history(self) -> NavHistory:
        return self._history

    def selection(self) -> list[tuple[str, bool]]:
        """Selected (name, is_dir) pairs, excluding the ".." row."""
        chosen: list[tuple[str, bool]] = []
        for index in self._table.selectionModel().selectedRows():
            item = self._table.item(index.row(), _NAME)
            if item is None:
                continue
            name, is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
            if not is_parent:
                chosen.append((name, is_dir))
        return chosen

    def selected_entries(self) -> list[RemoteEntry]:
        names = {name for name, _ in self.selection()}
        return [entry for entry in self._entries if entry.name in names]

    def entry(self, name: str) -> RemoteEntry | None:
        return next((item for item in self._entries if item.name == name), None)

    def set_busy(self, busy: bool) -> None:
        self._table.setEnabled(not busy)
        self._path_edit.setEnabled(not busy)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    # ----- navigation -----------------------------------------------------
    def refresh(self) -> None:
        self.navigate.emit(self._path)

    def go_back(self) -> None:
        target = self._history.back()
        if target:
            self._replay(target)

    def go_forward(self) -> None:
        target = self._history.forward()
        if target:
            self._replay(target)

    def _replay(self, target: str) -> None:
        """Navigate without disturbing the history stack."""
        self._replaying = True
        try:
            self.navigate.emit(target)
        finally:
            self._replaying = False
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self._back.setEnabled(self._history.can_go_back())
        self._forward.setEnabled(self._history.can_go_forward())
        self._recent.setEnabled(bool(self._history.entries))

    def _show_recent(self) -> None:
        menu = QMenu(self)
        entries = self._history.entries
        for index in range(len(entries) - 1, -1, -1):
            action = menu.addAction(entries[index])
            action.setData(index)
            if index == self._history.index:
                action.setCheckable(True)
                action.setChecked(True)
        chosen = menu.exec(self._recent.mapToGlobal(self._recent.rect().bottomLeft()))
        if chosen is None:
            return
        target = self._history.go(int(chosen.data()))
        if target:
            self._replay(target)

    def _on_double_click(self, row: int, _column: int) -> None:
        item = self._table.item(row, _NAME)
        if item is None:
            return
        name, is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
        if is_parent:
            self._go_up()
        elif is_dir:
            self.navigate.emit(self._child_path(name))

    def _go_up(self) -> None:
        self.navigate.emit(self._parent_path())

    def _on_menu(self, position) -> None:
        self.focused.emit()
        self.menu_requested.emit(self._table.viewport().mapToGlobal(position))

    # ----- drag and drop --------------------------------------------------
    # Dragging between the panes is how this layout is meant to be used: rows
    # carry their own MIME type so a drop knows which side they came from, and
    # dropping on a folder row lands inside that folder rather than in whichever
    # directory happens to be open. Local rows also carry file URLs, so they can
    # be dragged out to Explorer or an editor; remote rows have no real files to
    # offer, so they cannot leave the app.
    def _drag_payload(self) -> QMimeData | None:
        """What a drag out of this pane carries."""
        selection = self.selection()
        if not selection:
            return None
        data = QMimeData()
        payload = {
            "pane": id(self),
            "remote": self._posix,
            "base": self._path,
            "items": [[name, is_dir] for name, is_dir in selection],
        }
        data.setData(_ROWS_MIME, json.dumps(payload).encode("utf-8"))
        if not self._posix:
            data.setUrls(
                [QUrl.fromLocalFile(self._child_path(name)) for name, _ in selection]
            )
        return data

    def _on_drag(self, event) -> None:
        mime = event.mimeData()
        point = event.position().toPoint()
        if mime.hasFormat(_ROWS_MIME):
            payload = _decode_rows(mime)
            if payload is None or payload.get("pane") == id(self):
                self._end_drag()
                event.ignore()
                return
            self._track_drag(point)
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            self._track_drag(point)
            event.acceptProposedAction()
            return
        event.ignore()

    def _on_drag_leave(self, _event) -> None:
        self._end_drag()

    # A long listing cannot be dropped into without this: the folder you want
    # is below the fold, and a drag in progress cannot turn the wheel. Holding
    # near an edge scrolls, the way a file manager is expected to.
    def _track_drag(self, point) -> None:
        self._drag_point = point
        self._hover_at(point)
        height = self._table.viewport().height()
        if point.y() < _DRAG_EDGE:
            self._scroll_step = -1
        elif point.y() > height - _DRAG_EDGE:
            self._scroll_step = 1
        else:
            self._scroll_step = 0
        if self._scroll_step and not self._scroll_timer.isActive():
            self._scroll_timer.start()
        elif not self._scroll_step and self._scroll_timer.isActive():
            self._scroll_timer.stop()

    def _auto_scroll(self) -> None:
        """One step of the edge scroll, and re-aim at whatever is now there."""
        if not self._scroll_step:
            self._scroll_timer.stop()
            return
        bar = self._table.verticalScrollBar()
        before = bar.value()
        # A sixth of a screen per tick: a full page in about a third of a
        # second, and the same speed whether the bar counts pixels or rows.
        stride = max(1, bar.pageStep() // 6, bar.singleStep())
        bar.setValue(before + self._scroll_step * stride)
        if bar.value() == before:
            self._scroll_timer.stop()   # already at the end
            return
        if self._drag_point is not None:
            # The pointer has not moved but the rows under it have.
            self._hover_at(self._drag_point)

    def _end_drag(self) -> None:
        self._scroll_timer.stop()
        self._scroll_step = 0
        self._drag_point = None
        self._set_hover(-1)

    def _on_drop(self, event) -> None:
        mime = event.mimeData()
        target = self._drop_target(event.position().toPoint())
        self._end_drag()
        payload = _decode_rows(mime)
        if payload is not None:
            if payload.get("pane") == id(self):
                event.ignore()
                return
            event.acceptProposedAction()
            self.transfer_dropped.emit({**payload, "target": target})
            return
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit({"paths": paths, "target": target})

    def _drop_target(self, point) -> str:
        """Where a drop at this point belongs: a folder row, or this directory."""
        cell = self._table.itemAt(point)
        if cell is None:
            return self._path
        item = self._table.item(cell.row(), _NAME)
        if item is None:
            return self._path
        name, is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
        if is_parent:
            return self._parent_path()
        return self._child_path(name) if is_dir else self._path

    def _hover_at(self, point) -> None:
        cell = self._table.itemAt(point)
        item = self._table.item(cell.row(), _NAME) if cell is not None else None
        if item is None or cell is None:
            self._set_hover(-1)
            return
        _name, is_dir, _is_parent = item.data(Qt.ItemDataRole.UserRole)
        self._set_hover(cell.row() if is_dir else -1)

    def _set_hover(self, row: int) -> None:
        """Tint the folder a drop would land in, so the target is never a guess."""
        if row == self._hover_row:
            return
        for index, brush in (
            (self._hover_row, QBrush()),
            (row, QBrush(self._hover_colour)),
        ):
            if index < 0 or index >= self._table.rowCount():
                continue
            for column in range(self._table.columnCount()):
                cell = self._table.item(index, column)
                if cell is not None:
                    cell.setBackground(brush)
        self._hover_row = row if 0 <= row < self._table.rowCount() else -1

    # Path arithmetic differs per side; the owner installs the right callables.
    def bind_paths(self, child, parent) -> None:
        self._child_path = child
        self._parent_path = parent


class FileManagerTab(QWidget):
    """One FTP/FTPS/SFTP session as a dual-pane transfer view."""

    status_message = pyqtSignal(str)
    title_changed = pyqtSignal(str)
    #: Asks the window to open a shell tab for (profile, spec, directory).
    shell_requested = pyqtSignal(object, object, str)

    # Requests handed to the worker thread.
    _open_requested = pyqtSignal(object)
    _list_requested = pyqtSignal(str)
    _home_requested = pyqtSignal()
    _mkdir_requested = pyqtSignal(str)
    _delete_requested = pyqtSignal(str, bool)
    _rename_requested = pyqtSignal(str, str)
    _download_requested = pyqtSignal(object, str)
    _upload_requested = pyqtSignal(object, str)
    _close_requested = pyqtSignal()
    _chmod_requested = pyqtSignal(str, int, bool, str)
    _symlink_requested = pyqtSignal(str, str)
    _archive_requested = pyqtSignal(str, str, str, str)
    _extract_requested = pyqtSignal(str, str)
    _exec_requested = pyqtSignal(str, str)
    _undo_requested = pyqtSignal(str)
    _compare_requested = pyqtSignal(str, str, bool, object)
    _sync_scan_requested = pyqtSignal(str, str, bool, object, str, bool)
    _sync_delete_requested = pyqtSignal(object)
    _folder_stats_requested = pyqtSignal(str, object)
    _digest_requested = pyqtSignal(str)
    _grep_requested = pyqtSignal(str, str, bool, bool, str)
    _disk_usage_requested = pyqtSignal(str)
    _logs_requested = pyqtSignal(str)
    _pause_requested = pyqtSignal()
    _resume_requested = pyqtSignal()
    _cancel_item_requested = pyqtSignal(str)
    _prioritize_requested = pyqtSignal(str)
    _reorder_requested = pyqtSignal(object)
    _clear_finished_requested = pyqtSignal()
    _workers_requested = pyqtSignal(int)
    _options_changed = pyqtSignal(object)

    #: Marshals watcher callbacks (a plain thread) onto the GUI thread.
    _watch_changes = pyqtSignal(object)
    #: Marshals a synced folder's local changes onto the GUI thread.
    _sync_changes = pyqtSignal(str, object)
    #: Marshals a synced folder's git commits onto the GUI thread.
    _sync_commit = pyqtSignal(str, object)
    #: Marshals background local folder statistics onto the GUI thread.
    _local_stats_ready = pyqtSignal(str, object)

    def __init__(
        self,
        profile: ServerProfile,
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._settings = settings or Settings()
        self._connected = False
        self._closing = False
        self._remote_active = True
        self._queue_total = 0
        self._queue_done = 0
        self._capabilities: frozenset[Capability] = frozenset()
        self._history_store = HistoryStore()
        self._snippets = SnippetLibrary()
        self._stats_cache = FolderStatsCache()
        self._watcher: DirectoryWatcher | None = None
        #: Folders this connection keeps on the server, and their watchers.
        self._sync_store = SyncRuleStore()
        self._sync_watchers: dict[str, DirectoryWatcher] = {}
        self._git_watchers: dict[str, GitCommitWatcher] = {}
        #: Rules whose trigger fired while the connection was down.
        self._sync_pending: set[str] = set()
        #: One-shot rules made by "Sync now" on a folder that is not armed.
        self._sync_transient: dict[str, SyncRule] = {}
        #: Scope the next new rule gets, until told otherwise.
        self._sync_recursive_default = True
        #: Rules with a scan in flight, and how often each has been re-queued.
        self._sync_running: set[str] = set()
        self._sync_retries: dict[str, int] = {}
        self._diff_report = None
        self._diff_local = ""
        self._diff_remote = ""
        self._mirroring = False
        self._mirror_local_base = ""
        self._mirror_remote_base = ""
        self._command_history: list[str] = []
        self._dialogs: dict[str, QWidget] = {}
        self._spec = _spec_for(profile)

        self._build_ui()
        self.set_dark_mode(dark_mode)
        self._start_worker()
        self._install_shortcuts()
        self._load_local(profile.local_dir or os.path.expanduser("~"))
        self._start_sync_rules()
        self._connect_to_server()

    # ----- UI -------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if self._is_production:
            banner = QLabel("PRODUCTION — files here are live.")
            banner.setObjectName("banner")
            wrapper = QWidget()
            wrapper_layout = QHBoxLayout(wrapper)
            wrapper_layout.setContentsMargins(10, 7, 10, 0)
            wrapper_layout.addWidget(banner)
            layout.addWidget(wrapper)

        layout.addWidget(self._build_header())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 8)
        body_layout.setSpacing(8)
        layout.addWidget(body, 1)
        layout = body_layout  # everything below lives inside the padded body

        self._local = _FilePane("Local")
        self._local.bind_paths(self._local_child, self._local_parent)
        self._local.navigate.connect(self._load_local)
        self._local.focused.connect(lambda: self._set_active(remote=False))
        self._local.menu_requested.connect(lambda pos: self._show_menu(pos, remote=False))
        self._local.paths_dropped.connect(self._on_local_drop)
        self._local.transfer_dropped.connect(self._on_pane_drop)

        self._remote = _FilePane(f"Remote — {self._profile.label}", posix=True)
        self._remote.bind_paths(self._remote_child, self._remote_parent)
        self._remote.navigate.connect(self._list_remote)
        self._remote.focused.connect(lambda: self._set_active(remote=True))
        self._remote.menu_requested.connect(lambda pos: self._show_menu(pos, remote=True))
        self._remote.paths_dropped.connect(self._on_remote_drop)
        self._remote.transfer_dropped.connect(self._on_pane_drop)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.addWidget(self._local)
        panes.addWidget(self._remote)
        panes.setSizes([500, 500])
        layout.addWidget(panes, 1)

        self._queue_panel = TransferQueuePanel(workers=self._settings.transfer_workers)
        self._queue_panel.setVisible(False)
        self._queue_panel.pause_requested.connect(self._pause_requested.emit)
        self._queue_panel.resume_requested.connect(self._resume_requested.emit)
        self._queue_panel.cancel_all_requested.connect(self._on_cancel)
        self._queue_panel.cancel_item_requested.connect(self._cancel_item_requested.emit)
        self._queue_panel.prioritize_item_requested.connect(self._prioritize_requested.emit)
        self._queue_panel.reorder_requested.connect(self._reorder_requested.emit)
        self._queue_panel.clear_finished_requested.connect(self._on_clear_finished)
        self._queue_panel.workers_changed.connect(self._workers_requested.emit)
        layout.addWidget(self._queue_panel)

        self.layout().addWidget(self._build_footer())

        # The status line closes the tab off with its own top border, so the
        # window reads as header / work area / footer rather than one field.
        self._status = QLabel("Connecting…")
        self._status.setObjectName("status")
        self._status.setWordWrap(True)
        self.layout().addWidget(self._status)

    def _build_header(self) -> QWidget:
        """The session bar: what acts on the connection rather than a selection.

        Everything that needs a shell lives in one menu instead of seven loose
        buttons - the row used to wrap, and on a plain FTP session most of it
        was hidden anyway, which left a ragged gap.
        """
        bar = QWidget()
        bar.setObjectName("toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(6)

        self._mirror_box = QCheckBox("Mirror")
        self._mirror_box.setToolTip(
            "Keep both panes in step: entering a folder on one side enters the "
            "matching folder on the other"
        )
        self._mirror_box.setChecked(self._settings.mirror_navigation)
        row.addWidget(self._mirror_box)

        self._watch_box = QCheckBox("Watch")
        self._watch_box.setToolTip(self._watch_tooltip())
        self._watch_box.toggled.connect(self._on_watch_toggled)
        row.addWidget(self._watch_box)

        row.addSpacing(12)

        compare_btn = QPushButton("Compare")
        compare_btn.setToolTip("Hash both sides and show exactly what differs (F9)")
        compare_btn.clicked.connect(lambda: self._on_compare(with_hashes=True))
        row.addWidget(compare_btn)

        self._sync_btn = QToolButton()
        self._sync_btn.setObjectName("menubutton")
        self._sync_btn.setText("Sync  ▾")
        self._sync_btn.setToolTip(
            "Keep a local folder on the server: on every save, or on every git "
            "commit"
        )
        self._sync_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sync_menu = QMenu(self._sync_btn)
        sync_menu.aboutToShow.connect(lambda: self._fill_sync_menu(sync_menu))
        self._sync_btn.setMenu(sync_menu)
        row.addWidget(self._sync_btn)

        self._queue_btn = QPushButton("Queue")
        self._queue_btn.setCheckable(True)
        self._queue_btn.setToolTip("Show the transfer queue (Ctrl+Shift+Q)")
        self._queue_btn.toggled.connect(self._queue_panel_visible)
        row.addWidget(self._queue_btn)

        row.addSpacing(12)

        self._undo_btn = QPushButton("Undo replace")
        self._undo_btn.setToolTip("Restore the most recent file this app overwrote")
        self._undo_btn.clicked.connect(self._on_undo_last)
        row.addWidget(self._undo_btn)

        history_btn = QPushButton("History")
        history_btn.setToolTip("Files this app overwrote, and how to put them back")
        history_btn.clicked.connect(self._on_history)
        row.addWidget(history_btn)

        row.addStretch(1)

        self._server_btn = _menu_button(
            "Server tools",
            "Archives, search, disk usage, shell and logs - all run on the server",
            (
                ("Open a shell here", "Ctrl+T", self._on_terminal),
                ("Run a command…", "Ctrl+P", self._on_command_bar),
                ("Snippets…", "", self._on_snippets),
                (None, "", None),
                ("Search file contents…", "Ctrl+Shift+F", self._on_search),
                ("Disk usage…", "", self._on_disk_usage),
                ("Live logs…", "", self._on_logs),
                (None, "", None),
                ("Open in PuTTY / your terminal", "", self._on_external_terminal),
            ),
        )
        # Hidden until the connection says it really has a shell.
        self._server_btn.setVisible(False)
        self._shell_buttons: list[QWidget] = [self._server_btn]
        row.addWidget(self._server_btn)
        return bar

    def _watch_tooltip(self) -> str:
        if self._settings.watch_autosync:
            return "Notice local edits as they are saved, and upload them"
        return "Notice local edits as they are saved"

    def _build_footer(self) -> QWidget:
        """The transfer bar: what acts on the current selection."""
        bar = QWidget()
        bar.setObjectName("footerbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(6)

        self._upload_btn = QPushButton("▲ Upload")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.setToolTip("Copy the local selection to the remote directory")
        self._upload_btn.clicked.connect(self._on_upload)
        self._download_btn = QPushButton("▼ Download")
        self._download_btn.setToolTip("Copy the remote selection to the local directory")
        self._download_btn.clicked.connect(self._on_download)
        row.addWidget(self._upload_btn)
        row.addWidget(self._download_btn)

        row.addSpacing(12)

        mkdir_btn = QPushButton("New folder")
        mkdir_btn.clicked.connect(self._on_mkdir)
        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(self._on_rename)
        delete_btn = QPushButton("Delete")
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self._on_delete)
        for button in (mkdir_btn, rename_btn, delete_btn):
            row.addWidget(button)

        row.addSpacing(12)

        more = _menu_button(
            "More",
            "Less frequent actions",
            (
                ("Permissions…", "", self._on_permissions),
                ("Link target…", "", self._on_link_target),
                ("Digest…", "", self._on_digest_selected),
                (None, "", None),
                ("Archive on the server…", "", self._on_archive),
                ("Unpack here…", "", self._on_extract),
                (None, "", None),
                ("Browse local…", "", self._on_browse_local),
            ),
        )
        row.addWidget(more)

        row.addStretch(1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setFixedWidth(170)
        self._progress_label = QLabel("")
        self._progress_label.setObjectName("hint")
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._tool_progress = ToolProgressBar()
        self._tool_progress.cancel_requested.connect(self._on_cancel_tools)
        row.addWidget(self._progress_label)
        row.addWidget(self._progress)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._tool_progress)
        return bar

    def _install_shortcuts(self) -> None:
        pairs = (
            ("Alt+Left", lambda: self._active_pane().go_back()),
            ("Alt+Right", lambda: self._active_pane().go_forward()),
            ("Alt+Up", lambda: self._active_pane()._go_up()),
            ("F5", lambda: self._active_pane().refresh()),
            ("F9", lambda: self._on_compare(with_hashes=True)),
            ("Ctrl+T", self._on_terminal),
            ("Ctrl+P", self._on_command_bar),
            ("Ctrl+Shift+F", self._on_search),
            ("Ctrl+Z", self._on_undo_last),
            ("Ctrl+Shift+Q", lambda: self._queue_btn.toggle()),
            ("Ctrl+Shift+S", self._on_sync_current_now),
        )
        for keys, slot in pairs:
            QShortcut(QKeySequence(keys), self, slot)

    def set_dark_mode(self, enable: bool) -> None:
        self.setStyleSheet(theme.pane_stylesheet(enable))
        colours = theme.diff_colours(enable)
        for pane in (self._local, self._remote):
            pane.set_diff_colours(colours)
            pane.set_theme(enable)
        self._queue_panel.set_theme(enable)
        self._apply_diff_marks()

    def apply_settings(self, settings: Settings) -> None:
        """Take new preferences without reconnecting."""
        self._settings = settings
        self._mirror_box.setChecked(settings.mirror_navigation)
        self._watch_box.setToolTip(self._watch_tooltip())
        self._queue_panel.set_workers(settings.transfer_workers)
        self._options_changed.emit(self._pool_options())

    def current_title(self) -> str:
        return f"{self._profile.label} — {self._profile.kind.value.upper()}"

    @property
    def server_profile(self) -> ServerProfile:
        return self._profile

    @property
    def _is_production(self) -> bool:
        return self._profile.environment == Environment.PROD

    # ----- worker wiring --------------------------------------------------
    def _pool_options(self) -> PoolOptions:
        settings = self._settings
        return PoolOptions(
            workers=settings.transfer_workers,
            atomic=settings.atomic_uploads,
            keep_backups=settings.shadow_backups,
            overwrite=Overwrite.ALWAYS,
            verify=settings.verify_uploads,
        ).sane()

    def _ignore_rules(self) -> IgnoreRules:
        """The rules to apply to batch transfers and comparisons."""
        if not self._settings.use_ignore_rules:
            return IgnoreRules.empty()
        return IgnoreRules.from_local_dir(
            self._local.path or os.path.expanduser("~"),
            with_defaults=self._settings.ignore_defaults,
        )

    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = TransferWorker(
            options=self._pool_options(),
            history=self._history_store if self._settings.shadow_backups else None,
            profile_id=self._profile.id,
            profile_label=self._profile.label,
        )
        self._worker.moveToThread(self._thread)

        outgoing = (
            (self._open_requested, self._worker.open_connection),
            (self._list_requested, self._worker.list_dir),
            (self._home_requested, self._worker.request_home),
            (self._mkdir_requested, self._worker.make_dir),
            (self._delete_requested, self._worker.delete_entry),
            (self._rename_requested, self._worker.rename_entry),
            (self._download_requested, self._worker.run_download),
            (self._upload_requested, self._worker.run_upload),
            (self._close_requested, self._worker.close_connection),
            (self._chmod_requested, self._worker.request_chmod),
            (self._symlink_requested, self._worker.request_symlink),
            (self._archive_requested, self._worker.request_archive),
            (self._extract_requested, self._worker.request_extract),
            (self._exec_requested, self._worker.request_exec),
            (self._undo_requested, self._worker.request_undo),
            (self._compare_requested, self._worker.request_compare),
            (self._sync_scan_requested, self._worker.request_sync_scan),
            (self._sync_delete_requested, self._worker.delete_quietly),
            (self._folder_stats_requested, self._worker.request_folder_stats),
            (self._digest_requested, self._worker.request_digest),
            (self._grep_requested, self._worker.request_grep),
            (self._disk_usage_requested, self._worker.request_disk_usage),
            (self._logs_requested, self._worker.request_logs),
            (self._pause_requested, self._worker.pause_queue),
            (self._resume_requested, self._worker.resume_queue),
            (self._cancel_item_requested, self._worker.cancel_item),
            (self._prioritize_requested, self._worker.prioritize_item),
            (self._reorder_requested, self._worker.reorder_queue),
            (self._clear_finished_requested, self._worker.clear_finished),
            (self._workers_requested, self._worker.set_workers),
            (self._options_changed, self._worker.update_options),
        )
        for signal, slot in outgoing:
            signal.connect(slot)

        incoming = (
            (self._worker.connected, self._on_connected),
            (self._worker.capabilities_ready, self._on_capabilities),
            (self._worker.failed, self._on_failed),
            (self._worker.listing, self._on_listing),
            (self._worker.op_failed, self._on_op_message),
            (self._worker.op_done, self._on_op_message),
            (self._worker.queue_started, self._on_queue_started),
            (self._worker.progress, self._on_progress),
            (self._worker.file_finished, self._on_file_finished),
            (self._worker.queue_finished, self._on_queue_finished),
            (self._worker.queue_item, self._queue_panel.update_item),
            (self._worker.queue_stats, self._queue_panel.update_stats),
            (self._worker.tool_result, self._on_tool_result),
            (self._worker.tool_failed, self._on_tool_failed),
            (self._worker.tool_progress, self._on_tool_progress),
            (self._worker.closed, self._on_closed),
        )
        for signal, slot in incoming:
            signal.connect(slot)

        self._watch_changes.connect(self._on_watch_changes)
        self._sync_changes.connect(self._on_sync_changes)
        self._sync_commit.connect(self._on_sync_commit)
        self._local_stats_ready.connect(self._on_local_stats)
        self._thread.start()

    def _connect_to_server(self) -> None:
        profile = self._profile
        self._set_status(f"Connecting to {profile.describe_target()} …")
        self._open_requested.emit(self._spec)

    # ----- worker callbacks -----------------------------------------------
    def _on_connected(self, banner: str) -> None:
        self._connected = True
        self._set_status(banner)
        self.status_message.emit(f"Connected to {self._profile.label}")
        self.title_changed.emit(self.current_title())
        start = self._profile.remote_dir.strip()
        if start:
            self._list_remote(start)
        else:
            self._home_requested.emit()
        self._flush_pending_syncs()

    def _on_capabilities(self, capabilities: object) -> None:
        assert isinstance(capabilities, frozenset)
        self._capabilities = capabilities
        has_shell = Capability.EXEC in capabilities
        for button in self._shell_buttons:
            button.setVisible(has_shell)
        if not has_shell:
            self._set_status(
                self._status.text()
                + "  (file transfer only: this protocol has no shell, so the "
                "server-side tools are not available)"
            )

    def _on_failed(self, message: str) -> None:
        self._connected = False
        if self._closing:
            return
        self._set_status(message)
        self.status_message.emit(f"{self._profile.label}: {message}")
        QMessageBox.warning(self, "Connection failed", message)

    def _on_listing(self, path: str, entries: object) -> None:
        assert isinstance(entries, list)
        self._remote.set_listing(path, entries, at_root=path in ("/", ""))
        self._apply_diff_marks()
        self._request_remote_stats(path, entries)
        if self._mirror_box.isChecked():
            self._mirror_to_local(path)

    def _on_op_message(self, message: str) -> None:
        """Both success and failure of a single operation just report text."""
        self._set_status(message)
        self.status_message.emit(message)
        # An undo finishes on the worker thread, so this is the first moment the
        # History window can show the entry as restored.
        dialog = self._dialogs.get("history")
        if isinstance(dialog, HistoryDialog) and dialog.isVisible():
            dialog.refresh()
        self._sync_undo_button()

    def _on_queue_started(self, total: int) -> None:
        self._queue_total = total
        self._queue_done = 0
        self._progress.setVisible(total > 0)
        self._cancel_btn.setVisible(total > 0)
        self._progress.setValue(0)
        if total == 0:
            self._set_status("Nothing to transfer.")
        elif total > 1:
            self._queue_btn.setChecked(True)

    def _on_progress(self, name: str, transferred: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(transferred * 100 / total))
        else:
            # Unknown size: show an indeterminate bar rather than a fake number.
            self._progress.setRange(0, 0)
        position = f"{self._queue_done + 1}/{self._queue_total}" if self._queue_total else ""
        self._progress_label.setText(f"{position}  {name}  {_human_size(transferred)}")

    def _on_file_finished(self, _name: str) -> None:
        self._queue_done += 1

    def _on_queue_finished(self, completed: int, failed: int, cancelled: bool) -> None:
        self._progress.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._progress_label.setText("")
        parts = [f"{completed} file(s) transferred"]
        if failed:
            parts.append(f"{failed} failed")
        if cancelled:
            parts.append("cancelled")
        message = ", ".join(parts) + "."
        self._set_status(message)
        self.status_message.emit(message)
        self._stats_cache.invalidate()
        # The local side may have gained files; the remote refresh is done by
        # the worker itself.
        self._load_local(self._local.path)
        self._sync_undo_button()

    def _on_closed(self) -> None:
        self._connected = False
        self._set_status("Disconnected.")

    # ----- tool results ---------------------------------------------------
    def _on_tool_result(self, kind: str, payload: object) -> None:
        self._tool_progress.stop()
        handlers = {
            "folder_stats": self._apply_remote_stats,
            "compare": self._show_compare,
            "sync_scan": self._on_sync_scan,
            "digest": self._show_digest,
            "disk_usage": self._show_disk_usage,
            "grep": self._show_grep,
            "logs": self._show_logs,
            "exec": self._show_exec,
        }
        handler = handlers.get(kind)
        if handler is not None:
            handler(payload)

    def _on_tool_failed(self, kind: str, message: str) -> None:
        self._tool_progress.stop()
        if kind == "sync_scan":
            self._on_sync_scan_failed(message)
            return
        dialog = self._dialogs.get(kind)
        if dialog is not None and hasattr(dialog, "show_error"):
            dialog.show_error(message)  # type: ignore[attr-defined]
        if kind != "folder_stats":
            self._set_status(message)

    def _on_tool_progress(self, _kind: str, text: str) -> None:
        self._tool_progress.update_text(text)

    def _on_cancel_tools(self) -> None:
        self._worker.cancel_tools()
        self._tool_progress.stop()
        self._set_status("Stopping…")

    # ----- folder statistics ---------------------------------------------
    def _request_remote_stats(self, path: str, entries: list) -> None:
        if not self._settings.folder_stats:
            return
        names = [entry.name for entry in entries if entry.is_dir and not entry.is_link]
        if not names or len(names) > _MAX_STAT_FOLDERS:
            return
        cached = {
            name: self._stats_cache.get("remote", RemoteFS.join(path, name))
            for name in names
        }
        if all(value is not None for value in cached.values()):
            self._apply_remote_stats({"parent": path, "stats": cached})
            return
        self._folder_stats_requested.emit(path, names)

    def _apply_remote_stats(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload.get("parent") != self._remote.path:
            return
        stats = payload.get("stats") or {}
        for name, value in stats.items():
            if value is not None:
                self._stats_cache.put(
                    "remote", RemoteFS.join(self._remote.path, name), value
                )
        from mysql_runner.transfer.treestat import apply_folder_stats

        self._remote.update_entries(apply_folder_stats(self._remote.entries, stats))
        self._apply_diff_marks()

    def _request_local_stats(self, path: str, entries: list) -> None:
        if not self._settings.folder_stats:
            return
        names = [entry.name for entry in entries if entry.is_dir and not entry.is_link]
        if not names or len(names) > _MAX_STAT_FOLDERS:
            return

        def measure() -> None:
            found = {}
            for name in names:
                full = os.path.join(path, name)
                cached = self._stats_cache.get("local", full)
                if cached is None:
                    try:
                        cached = local_folder_stats(full)
                    except OSError:
                        continue
                    self._stats_cache.put("local", full, cached)
                found[name] = cached
            self._local_stats_ready.emit(path, found)

        threading.Thread(target=measure, name="local-stats", daemon=True).start()

    def _on_local_stats(self, path: str, stats: object) -> None:
        if path != self._local.path or not isinstance(stats, dict):
            return
        from mysql_runner.transfer.treestat import apply_folder_stats

        self._local.update_entries(apply_folder_stats(self._local.entries, stats))
        self._apply_diff_marks()

    # ----- local side -----------------------------------------------------
    def _load_local(self, path: str) -> None:
        target = os.path.abspath(os.path.expanduser(path or "~"))
        if not os.path.isdir(target):
            self._set_status(f"{target} is not a directory.")
            return
        try:
            entries = _scan_local(target)
        except OSError as exc:
            self._set_status(f"Cannot read {target}: {exc}")
            return
        at_root = os.path.dirname(target) == target
        self._local.set_listing(target, entries, at_root=at_root)
        self._apply_diff_marks()
        self._request_local_stats(target, entries)
        if self._mirror_box.isChecked():
            self._mirror_to_remote(target)
        self._apply_sync_marks()
        if self._watcher is not None and self._watcher.root != target:
            self._restart_watcher(target)

    def _local_child(self, name: str) -> str:
        return os.path.join(self._local.path, name)

    def _local_parent(self) -> str:
        return os.path.dirname(self._local.path.rstrip("\\/")) or self._local.path

    # ----- remote side ----------------------------------------------------
    def _list_remote(self, path: str) -> None:
        if not self._connected:
            self._set_status("Not connected.")
            return
        self._list_requested.emit(path or "/")

    def _remote_child(self, name: str) -> str:
        return RemoteFS.join(self._remote.path, name)

    def _remote_parent(self) -> str:
        return RemoteFS.parent(self._remote.path)

    # ----- mirrored navigation -------------------------------------------
    def _mirror_to_local(self, remote_path: str) -> None:
        if self._mirroring or not self._diff_bases_known():
            return
        target = mirror_path(self._mirror_remote_base, remote_path,
                             self._mirror_local_base, posix=False)
        if not target or target == self._local.path or not os.path.isdir(target):
            return
        self._mirroring = True
        try:
            self._load_local(target)
        finally:
            self._mirroring = False

    def _mirror_to_remote(self, local_path: str) -> None:
        if self._mirroring or not self._diff_bases_known():
            return
        target = mirror_path(self._mirror_local_base, local_path,
                             self._mirror_remote_base, posix=True)
        if not target or target == self._remote.path:
            return
        self._mirroring = True
        try:
            self._list_remote(target)
        finally:
            self._mirroring = False

    def _diff_bases_known(self) -> bool:
        """Anchor mirroring at whichever pair of directories was paired first."""
        if not self._mirror_local_base or not self._mirror_remote_base:
            if self._local.path and self._remote.path:
                self._mirror_local_base = self._local.path
                self._mirror_remote_base = self._remote.path
            else:
                return False
        return True

    # ----- comparison ----------------------------------------------------
    def _on_compare(self, *, with_hashes: bool = True) -> None:
        if not self._require_connection():
            return
        if not self._local.path:
            return
        self._tool_progress.start("Comparing…")
        self._set_status("Comparing both sides…")
        self._compare_requested.emit(
            self._local.path, self._remote.path or "/", with_hashes, self._ignore_rules()
        )

    def _show_compare(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._diff_report = payload.get("report")
        self._diff_local = payload.get("local_dir", "")
        self._diff_remote = payload.get("remote_dir", "")
        self._apply_diff_marks()
        dialog = self._dialogs.get("compare")
        if isinstance(dialog, CompareDialog):
            dialog.set_report(payload)
            dialog.raise_()
            return
        dialog = CompareDialog({**payload, "dark": self._settings.dark_mode}, self)
        dialog.upload_requested.connect(self._upload_relative)
        dialog.download_requested.connect(self._download_relative)
        dialog.refresh_requested.connect(
            lambda hashes: self._on_compare(with_hashes=hashes)
        )
        self._dialogs["compare"] = dialog
        dialog.show()
        if self._diff_report is not None:
            self._set_status(self._diff_report.summary())

    def _apply_diff_marks(self) -> None:
        report = self._diff_report
        if report is None:
            return

        def local_rel(name: str):
            base = self._local.path
            if not base or not self._diff_local:
                return None
            full = os.path.join(base, name)
            rel = os.path.relpath(full, self._diff_local)
            return None if rel.startswith("..") else rel.replace("\\", "/")

        def remote_rel(name: str):
            base = self._remote.path
            if not base or not self._diff_remote:
                return None
            full = RemoteFS.join(base, name)
            prefix = self._diff_remote.rstrip("/") + "/"
            if full == self._diff_remote:
                return ""
            return full[len(prefix):] if full.startswith(prefix) else None

        self._local.set_diff(report, local_rel)
        self._remote.set_diff(report, remote_rel)

    def _upload_relative(self, relatives: object) -> None:
        """Upload files named relative to the compared local directory."""
        if not isinstance(relatives, list) or not self._require_connection():
            return
        base = self._diff_local or self._local.path
        items = [
            (os.path.join(base, rel.replace("/", os.sep)), False) for rel in relatives
        ]
        existing = [item for item in items if os.path.exists(item[0])]
        if not existing:
            self._set_status("Those files are no longer here.")
            return
        if not self._confirm_production(f"replace {len(existing)} file(s) on"):
            return
        self._upload_tree(existing, self._diff_remote or self._remote.path, flatten=base)

    def _download_relative(self, relatives: object) -> None:
        if not isinstance(relatives, list) or not self._require_connection():
            return
        base = self._diff_remote or self._remote.path
        items = [(RemoteFS.join(base, rel), False) for rel in relatives]
        self._download_tree(items, self._diff_local or self._local.path, flatten=base)

    def _upload_tree(
        self, items, remote_dir: str, *, flatten: str = "", rules=None
    ) -> None:
        """Send items, keeping their layout relative to ``flatten`` if given."""
        ignore = rules if rules is not None else self._ignore_rules()
        if not flatten:
            self._upload_requested.emit((items, ignore), remote_dir)
            return
        # Group by their sub-directory so nested files land in the right place.
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for path, is_dir in items:
            rel_dir = os.path.dirname(os.path.relpath(path, flatten))
            target = RemoteFS.join(remote_dir, rel_dir.replace("\\", "/")) if rel_dir else remote_dir
            by_dir.setdefault(target, []).append((path, is_dir))
        for target, group in by_dir.items():
            self._upload_requested.emit((group, ignore), target)

    def _download_tree(self, items, local_dir: str, *, flatten: str = "") -> None:
        if not flatten:
            self._download_requested.emit((items, self._ignore_rules()), local_dir)
            return
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for path, is_dir in items:
            rel = path[len(flatten.rstrip("/")) + 1:] if path.startswith(flatten) else ""
            rel_dir = os.path.dirname(rel)
            target = os.path.join(local_dir, rel_dir.replace("/", os.sep)) if rel_dir else local_dir
            by_dir.setdefault(target, []).append((path, is_dir))
        for target, group in by_dir.items():
            os.makedirs(target, exist_ok=True)
            self._download_requested.emit((group, self._ignore_rules()), target)

    # ----- the watcher ---------------------------------------------------
    def _on_watch_toggled(self, watching: bool) -> None:
        if watching:
            self._restart_watcher(self._local.path)
        elif self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            self._set_status("Stopped watching.")

    def _restart_watcher(self, path: str) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if not path or not os.path.isdir(path):
            return
        self._watcher = DirectoryWatcher(
            path,
            self._watch_changes.emit,
            rules=self._ignore_rules(),
            on_message=self.status_message.emit,
        )
        self._watcher.start(prime=True)
        auto = " and uploading changes" if self._settings.watch_autosync else ""
        self._set_status(f"Watching {path}{auto}.")

    def _on_watch_changes(self, changes: object) -> None:
        if not isinstance(changes, list) or not changes:
            return
        self._load_local(self._local.path)
        text = summarise(changes)
        covering = self._sync_store.owner(self._profile.id, self._local.path)
        if covering is not None and covering.mode is SyncMode.ON_SAVE:
            # The synced folder is already uploading these; saying so beats
            # uploading everything twice.
            self._set_status(f"Changed locally: {text} (handled by the sync)")
            return
        if not self._settings.watch_autosync:
            self._set_status(f"Changed locally: {text}")
            return
        if not self._connected:
            self._set_status(f"Changed locally, but not connected: {text}")
            return
        self._upload_changes(changes)

    def _upload_changes(self, changes: list[Change]) -> None:
        base = self._local.path
        wanted = [
            change
            for change in changes
            if change.kind in (ChangeKind.ADDED, ChangeKind.MODIFIED)
            and os.path.isfile(change.path)
        ]
        if not wanted:
            return
        if self._is_production and self._settings.production_guard:
            # Auto-upload to production is exactly the accident this guard is
            # for, so it asks - once per batch, not once per file.
            if not self._confirm_production(f"upload {len(wanted)} changed file(s) to"):
                self._watch_box.setChecked(False)
                return
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for change in wanted:
            rel_dir = os.path.dirname(change.rel)
            target = (
                RemoteFS.join(self._remote.path, rel_dir) if rel_dir else self._remote.path
            )
            by_dir.setdefault(target, []).append((change.path, False))
        for target, group in by_dir.items():
            self._upload_requested.emit((group, self._ignore_rules()), target)
        self._set_status(f"Uploading {len(wanted)} changed file(s) from {base}.")

    # ----- synced folders -------------------------------------------------
    # A synced folder is a local directory paired with a remote one and a
    # trigger. On save, each file goes up as soon as its size and timestamp stop
    # moving. On git commit, the whole folder is compared with the server and
    # brought into step - which is what a commit means: not "this file changed"
    # but "this tree is now the one I want deployed". Rules are stored per
    # connection and armed again when the tab is reopened, and every removal
    # they perform is confined to the folder the rule names.
    def _sync_rules(self) -> list[SyncRule]:
        return self._sync_store.for_profile(self._profile.id)

    def _rule(self, rule_id: str) -> SyncRule | None:
        """A stored rule, or a one-shot rule made for a single Sync now."""
        return self._sync_store.get(rule_id) or self._sync_transient.get(rule_id)

    def _armed_rules(self) -> set[str]:
        return set(self._sync_watchers) | set(self._git_watchers)

    def _rule_ignores(self, rule: SyncRule) -> IgnoreRules:
        """The ignore rules for one synced folder, read from that folder."""
        if not (rule.use_ignore_rules and self._settings.use_ignore_rules):
            return IgnoreRules.empty()
        return IgnoreRules.from_local_dir(
            rule.local, with_defaults=self._settings.ignore_defaults
        )

    def _sync_with_hashes(self) -> bool:
        """Whether an automatic sync should hash both sides.

        Uploads carry the local timestamp over wherever the server allows it, so
        size and time is both accurate and enormously faster than reading every
        byte of a tree. Where the server cannot be given a timestamp it stamps
        its own upload time on everything, which would make every file look
        changed on the next commit - there, hashes are the only honest answer.
        """
        return Capability.SET_MTIME not in self._capabilities

    # ----- arming and disarming -------------------------------------------
    def _start_sync_rules(self) -> None:
        for rule in self._sync_rules():
            self._start_sync_rule(rule)
        self._apply_sync_marks()

    def _start_sync_rule(self, rule: SyncRule) -> None:
        """Give one rule the watcher its trigger needs."""
        self._stop_sync_rule(rule.id)
        if not rule.mode.is_live:
            return
        if not os.path.isdir(rule.local):
            self._set_status(f"{rule.local} is not on this machine; sync idle.")
            return
        if rule.mode is SyncMode.ON_SAVE:
            watcher = DirectoryWatcher(
                rule.local,
                lambda changes, rid=rule.id: self._sync_changes.emit(rid, changes),
                rules=self._rule_ignores(rule),
                on_message=self.status_message.emit,
                recursive=rule.recursive,
            )
            watcher.start(prime=True)
            self._sync_watchers[rule.id] = watcher
            return
        watcher = GitCommitWatcher(
            rule.local,
            lambda event, rid=rule.id: self._sync_commit.emit(rid, event),
            on_message=self.status_message.emit,
        )
        if not watcher.valid:
            self._set_status(
                f"{rule.name} is not inside a git repository, so there are no "
                "commits to watch."
            )
            return
        watcher.start(prime=True)
        self._git_watchers[rule.id] = watcher

    def _stop_sync_rule(self, rule_id: str) -> None:
        watcher = self._sync_watchers.pop(rule_id, None)
        if watcher is not None:
            watcher.stop()
        git = self._git_watchers.pop(rule_id, None)
        if git is not None:
            git.stop()

    def _selected_local_dir(self) -> str:
        """The folder the sync commands act on: the one picked, or the one open."""
        selection = self._local.selection()
        folders = [name for name, is_dir in selection if is_dir]
        if len(folders) == 1:
            return self._local_child(folders[0])
        return self._local.path

    def _remote_target_for(self, local: str) -> str:
        """Where a local folder belongs on the server, from what is on screen."""
        remote_base = self._remote.path or self._profile.remote_dir.strip() or "/"
        base_local = self._local.path
        if not base_local or not local:
            return remote_base
        rel = os.path.relpath(local, base_local)
        if rel in (".", "") or rel.startswith(".."):
            return remote_base
        return RemoteFS.join(remote_base, rel.replace("\\", "/"))

    def _arm_sync(self, local: str, mode: SyncMode) -> None:
        """Mark a folder as synced, then bring it into step straight away."""
        if not local or not os.path.isdir(local):
            self._set_status("Pick a local folder to sync.")
            return
        rule = self._sync_store.find(self._profile.id, local)
        nested = [
            other
            for other in self._sync_rules()
            if other.local != local and other.covers(local) is False
            and _inside(other.local, local)
        ]
        if rule is None:
            remote = self._remote_target_for(local)
            if not remote:
                self._set_status("Connect first, so the folder has a server side.")
                return
            # A folder that already has synced folders under it is almost
            # always a site root: sync the loose files at the top and leave the
            # subfolders to the rules that already own them.
            rule = SyncRule(
                profile_id=self._profile.id,
                local=local,
                remote=remote,
                mode=mode,
                recursive=self._sync_recursive_default and not nested,
            )
        else:
            rule.mode = mode
        rule = self._sync_store.put(rule)
        self._sync_transient.pop(rule.id, None)
        self._start_sync_rule(rule)
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        if mode is SyncMode.ON_COMMIT and not describe_repo(rule.local):
            QMessageBox.information(
                self,
                "Not a git repository",
                f"{rule.local} is not inside a git work tree, so no commit can "
                "be seen from there. The folder is remembered - switch it to "
                "On save, or point it at a checkout.",
            )
            return
        repo = describe_repo(rule.local) if mode is SyncMode.ON_COMMIT else ""
        trigger = f"on each commit in {repo}" if repo else mode.label.lower()
        because = (
            f" - files in it only, since {len(nested)} folder(s) under it are "
            "synced in their own right"
            if nested and not rule.recursive
            else f" ({rule.scope})"
        )
        self._set_status(
            f"Syncing {rule.local} to {rule.remote} {trigger}{because}."
        )
        self.status_message.emit(f"{self._profile.label}: syncing {rule.name} {trigger}")
        self._sync_now(rule)

    def _stop_sync_folder(self, local: str) -> None:
        rule = self._sync_store.find(self._profile.id, local)
        if rule is None:
            return
        self._on_sync_forget(rule.id)

    def _on_sync_forget(self, rule_id: str) -> None:
        rule = self._sync_store.get(rule_id)
        self._stop_sync_rule(rule_id)
        self._sync_store.remove(rule_id)
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        if rule is not None:
            self._set_status(f"Stopped syncing {rule.local}.")

    def _on_sync_mode_changed(self, rule_id: str, value: str) -> None:
        try:
            mode = SyncMode(value)
        except ValueError:
            return
        rule = self._sync_store.set_mode(rule_id, mode)
        if rule is None:
            return
        self._start_sync_rule(rule)
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        self._set_status(f"{rule.name}: {rule.mode.label.lower()}.")

    def _toggle_sync_scope(self, local: str) -> None:
        """Flip whether a folder's rule reaches into its subfolders.

        With no rule on the folder yet this sets what the next one will be, so
        the choice can be made before arming rather than corrected after.
        """
        rule = self._sync_store.find(self._profile.id, local) if local else None
        if rule is None:
            self._sync_recursive_default = not self._sync_recursive_default
            scope = "with subfolders" if self._sync_recursive_default else (
                "the files in it only"
            )
            self._set_status(f"The next folder you sync will cover {scope}.")
            return
        self._on_sync_scope_changed(rule.id, not rule.recursive)

    def _on_sync_scope_changed(self, rule_id: str, recursive: bool) -> None:
        rule = self._sync_store.set_flag(rule_id, "recursive", recursive)
        if rule is None:
            return
        self._start_sync_rule(rule)   # the watcher has a different scope now
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        self._set_status(f"{rule.name}: syncing {rule.scope}.")

    def _on_sync_removals_changed(self, rule_id: str, mirror: bool) -> None:
        rule = self._sync_store.set_flag(rule_id, "delete_remote", mirror)
        if rule is None:
            return
        self._refresh_sync_dialog()
        state = "mirrored" if mirror else "left alone"
        self._set_status(f"{rule.name}: removals are {state}.")

    # ----- running a sync -------------------------------------------------
    def _sync_folder_now(self, local: str) -> None:
        """Reconcile a folder once, whether or not it is armed."""
        if not local or not os.path.isdir(local):
            self._set_status("Pick a local folder to sync.")
            return
        rule = self._sync_store.find(self._profile.id, local)
        if rule is None:
            rule = SyncRule(
                profile_id=self._profile.id,
                local=local,
                remote=self._remote_target_for(local),
                mode=SyncMode.OFF,
            )
            self._sync_transient[rule.id] = rule
        self._sync_now(rule)

    def _on_sync_current_now(self) -> None:
        self._sync_folder_now(self._selected_local_dir())

    def _sync_all_now(self) -> None:
        for rule in self._sync_rules():
            self._sync_now(rule)

    def _sync_now(self, rule: SyncRule | None) -> None:
        """Ask for a full comparison of one synced folder."""
        if rule is None or self._closing:
            return
        if not os.path.isdir(rule.local):
            self._set_status(f"{rule.local} is not on this machine any more.")
            return
        if not self._connected:
            self._sync_pending.add(rule.id)
            self._set_status(f"{rule.name}: will sync once the connection is up.")
            return
        if rule.id in self._sync_running:
            return
        self._sync_running.add(rule.id)
        self._tool_progress.start(f"Syncing {rule.name}…")
        self._set_status(f"{rule.name}: comparing with {rule.remote}…")
        self._sync_scan_requested.emit(
            rule.local,
            rule.remote,
            self._sync_with_hashes(),
            self._rule_ignores(rule),
            rule.id,
            rule.recursive,
        )

    def _flush_pending_syncs(self) -> None:
        """Run the syncs whose trigger fired while the connection was down."""
        waiting, self._sync_pending = sorted(self._sync_pending), set()
        for rule_id in waiting:
            self._sync_now(self._rule(rule_id))

    def _on_sync_changes(self, rule_id: str, changes: object) -> None:
        """A synced folder in on-save mode saw files settle on disk."""
        rule = self._rule(rule_id)
        if rule is None or not isinstance(changes, list) or not changes:
            return
        if rule.covers(self._local.path):
            self._load_local(self._local.path)
        # Folders can be nested - a repository synced whole, one of its
        # directories synced somewhere else - and both watchers see the same
        # save. The innermost rule owns it, so the file goes up once.
        changes = [
            change for change in changes if self._owns_change(rule, change.path)
        ]
        if not changes:
            return
        uploads = [
            change
            for change in changes
            if change.kind in (ChangeKind.ADDED, ChangeKind.MODIFIED)
            and os.path.isfile(change.path)
        ]
        removals = (
            [change for change in changes if change.kind is ChangeKind.REMOVED]
            if rule.delete_remote
            else []
        )
        if not uploads and not removals:
            return
        if not self._connected:
            self._sync_pending.add(rule.id)
            self._set_status(
                f"{rule.name}: {summarise(changes)} - waiting for the connection."
            )
            return
        if self._is_production and self._settings.production_guard:
            # Uploading to production the moment a file is saved is exactly the
            # accident this guard exists for, so it asks once per batch. Saying
            # no pauses the rule rather than dropping it.
            what = f"sync {len(uploads)} change(s) and {len(removals)} removal(s) to"
            if not self._confirm_production(what):
                self._sync_store.set_mode(rule.id, SyncMode.OFF)
                self._stop_sync_rule(rule.id)
                self._apply_sync_marks()
                self._refresh_sync_dialog()
                self._set_status(f"{rule.name}: sync paused.")
                return
        if uploads:
            self._upload_tree(
                [(change.path, False) for change in uploads],
                rule.remote,
                flatten=rule.local,
                rules=self._rule_ignores(rule),
            )
        gone = [rule.remote_for(change.path) for change in removals]
        gone = [path for path in gone if path and path != rule.remote]
        if gone:
            # A watched removal is unambiguous - the file was there and the user
            # deleted it - so this one does not stop to ask.
            self._sync_delete_requested.emit(gone)
        self._set_status(
            f"{rule.name}: {summarise(changes)} → {rule.remote}"
        )

    def _owns_change(self, rule: SyncRule, path: str) -> bool:
        """Whether this rule, not a deeper one, is the one to act on a path."""
        if not rule.owns(path):
            return False  # below a files-only rule: not this rule's business
        owner = self._sync_store.owner(self._profile.id, path)
        if owner is None or owner.id == rule.id or not owner.mode.is_live:
            return True
        return len(owner.local) <= len(rule.local)

    def _on_sync_commit(self, rule_id: str, event: object) -> None:
        """A synced folder in on-commit mode saw its repository move."""
        rule = self._rule(rule_id)
        if rule is None:
            return
        detail = event.describe() if isinstance(event, CommitEvent) else ""
        headline = f"{rule.name}: commit {detail}" if detail else f"{rule.name}: commit"
        self._set_status(f"{headline} - syncing…")
        self.status_message.emit(f"{self._profile.label}: {headline}")
        self._sync_now(rule)

    def _on_sync_scan(self, payload: object) -> None:
        """A comparison for one synced folder came back: act on it."""
        if not isinstance(payload, dict):
            return
        rule_id = str(payload.get("rule_id", ""))
        self._sync_running.discard(rule_id)
        self._sync_retries.pop(rule_id, None)
        rule = self._rule(rule_id)
        report = payload.get("report")
        if rule is None or report is None:
            return
        self._sync_transient.pop(rule_id, None)
        uploads = [
            os.path.join(rule.local, rel.replace("/", os.sep))
            for rel in report.to_upload()
        ]
        uploads = [path for path in uploads if os.path.isfile(path)]
        removals = self._removal_paths(rule, report) if rule.delete_remote else []
        if not uploads and not removals:
            self._set_status(
                f"{rule.name}: already in step with {rule.remote} "
                f"(compared by {report.compared_by})."
            )
            return
        if uploads and not self._confirm_production(
            f"upload {len(uploads)} file(s) to"
        ):
            return
        removing = bool(removals) and self._confirm_removals(rule, removals)
        if uploads:
            self._upload_tree(
                [(path, False) for path in uploads],
                rule.remote,
                flatten=rule.local,
                rules=self._rule_ignores(rule),
            )
        if removing:
            self._sync_delete_requested.emit(removals)
        parts = []
        if uploads:
            parts.append(f"uploading {len(uploads)} file(s)")
        if removing:
            parts.append(f"removing {len(removals)} item(s)")
        elif removals:
            parts.append(f"leaving {len(removals)} server-only item(s) alone")
        self._set_status(f"{rule.name}: " + ", ".join(parts) + ".")

    def _on_sync_scan_failed(self, message: str) -> None:
        """A scan could not run.

        The tool channel takes one job at a time, so a sync that lands during a
        comparison is re-queued instead of being lost.
        """
        waiting, self._sync_running = sorted(self._sync_running), set()
        busy = "busy" in message.lower()
        for rule_id in waiting:
            attempts = self._sync_retries.get(rule_id, 0)
            if busy and attempts < 5 and not self._closing:
                self._sync_retries[rule_id] = attempts + 1
                # Parented to this widget, so a tab closed in the meantime
                # takes its pending retries with it.
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(
                    lambda rid=rule_id: self._sync_now(self._rule(rid))
                )
                timer.start(4000)
                continue
            self._sync_retries.pop(rule_id, None)
            rule = self._rule(rule_id)
            self._set_status(f"{rule.name if rule else 'Sync'}: {message}")
        if busy and waiting:
            self._set_status("Sync is waiting for the running job to finish…")

    def _removal_paths(self, rule: SyncRule, report) -> list[str]:
        """What a full sync would remove, confined to this folder.

        Only paths inside the rule's own remote folder are ever returned: a rule
        deletes within the folder it was given and nowhere else. Directories the
        local side no longer has are collapsed to their topmost parent, and the
        files inside them need no separate delete.
        """
        remote_tree = getattr(report, "remote", None)
        local_tree = getattr(report, "local", None)
        tops: list[str] = []
        if rule.recursive and remote_tree is not None and local_tree is not None:
            for rel in sorted(remote_tree.dirs):
                if not rel or rel in local_tree.dirs:
                    continue
                if any(rel == top or rel.startswith(f"{top}/") for top in tops):
                    continue
                tops.append(rel)
        files = [
            rel
            for rel in report.paths(DiffStatus.REMOTE_ONLY)
            if not any(rel.startswith(f"{top}/") for top in tops)
            # A files-only rule never looked inside the subfolders, so it has
            # no business removing anything it finds there.
            and (rule.recursive or "/" not in rel)
        ]
        prefix = "/" if rule.remote == "/" else f"{rule.remote}/"
        paths = [RemoteFS.join(rule.remote, rel) for rel in tops + files]
        return [
            path for path in paths if path.startswith(prefix) and path != rule.remote
        ]

    def _confirm_removals(self, rule: SyncRule, paths: list[str]) -> bool:
        """Ask before a full sync removes files that only the server has.

        A comparison cannot tell "you deleted this" from "the server wrote
        this": an uploads folder, a log or a cache looks exactly like a file that
        used to be local. So the first removal on a folder asks, with the list,
        and remembers the answer for that folder - while an unusually large
        batch asks again even when the answer was yes.
        """
        if rule.auto_remove and len(paths) <= _BULK_REMOVAL:
            return True
        if not self._confirm_production(f"remove {len(paths)} item(s) from"):
            return False
        box = QMessageBox(self)
        box.setWindowTitle("Remove what only the server has?")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"{rule.name}: {len(paths)} item(s) are on the server but not in "
            f"{rule.local}."
        )
        box.setInformativeText(
            "Removing them makes the server match the folder exactly. Anything "
            "the server produces itself - uploads, logs, caches - belongs in "
            ".deployignore instead, and then no sync will touch it."
        )
        box.setDetailedText("\n".join(paths[:200]))
        remember = QCheckBox("Remove these without asking from now on")
        box.setCheckBox(remember)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return False
        if remember.isChecked():
            self._sync_store.set_flag(rule.id, "auto_remove", True)
        return True

    # ----- showing what is synced -----------------------------------------
    def _apply_sync_marks(self) -> None:
        """Mark synced folders in the local pane and count them on the button."""
        rules = self._sync_rules()
        base = self._local.path
        marks: dict[str, str] = {}
        for rule in rules:
            parent = os.path.dirname(rule.local)
            if base and os.path.normcase(parent) == os.path.normcase(base):
                marks[os.path.basename(rule.local)] = (
                    f"Synced to {rule.remote} - {rule.mode.label.lower()}, "
                    f"{rule.scope}"
                )
        self._local.set_sync_marks(marks)
        here = self._sync_store.find(self._profile.id, base) if base else None
        self._local.set_title(
            f"Local — synced {here.mode.label.lower()}, {here.scope}"
            if here
            else "Local"
        )
        if hasattr(self, "_sync_btn"):
            live = len(self._armed_rules())
            self._sync_btn.setText(
                f"Sync ({live})  ▾" if live else "Sync  ▾"
            )

    def _fill_sync_menu(self, menu) -> None:
        """Build the sync menu for whichever folder is in play."""
        menu.clear()
        local = self._selected_local_dir()
        rule = self._sync_store.find(self._profile.id, local) if local else None
        name = (os.path.basename(local) or local) if local else "this folder"
        title = menu.addAction(f"Folder: {name}")
        title.setEnabled(False)
        sync_now = menu.addAction(
            "Sync now (upload changes)", lambda: self._sync_folder_now(local)
        )
        sync_now.setEnabled(bool(local))
        menu.addSeparator()
        for mode, label in (
            (SyncMode.ON_SAVE, "Keep in sync — on save"),
            (SyncMode.ON_COMMIT, "Keep in sync — on git commit"),
        ):
            action = menu.addAction(label, lambda m=mode: self._arm_sync(local, m))
            action.setCheckable(True)
            action.setChecked(rule is not None and rule.mode is mode)
            action.setEnabled(bool(local))
        scope = menu.addAction(
            "Include subfolders", lambda: self._toggle_sync_scope(local)
        )
        scope.setCheckable(True)
        scope.setChecked(
            rule.recursive if rule is not None else self._sync_recursive_default
        )
        scope.setEnabled(bool(local))
        scope.setToolTip(
            "Off syncs the files sitting in this folder and nothing below it - "
            "which is how a site root is synced without dragging every "
            "subfolder up with it"
        )
        menu.addSeparator()
        pause = menu.addAction(
            "Pause this folder", lambda: self._on_sync_mode_changed(
                rule.id if rule else "", SyncMode.OFF.value
            )
        )
        pause.setEnabled(rule is not None and rule.mode.is_live)
        stop = menu.addAction(
            "Stop syncing this folder", lambda: self._stop_sync_folder(local)
        )
        stop.setEnabled(rule is not None)
        menu.addSeparator()
        rules = self._sync_rules()
        if rules:
            listed = menu.addMenu(f"Folders synced here ({len(rules)})")
            for other in rules:
                listed.addAction(
                    f"{other.local} — {other.mode.label}, {other.scope}",
                    lambda rid=other.id: self._sync_now(self._rule(rid)),
                )
            menu.addAction("Sync all of them now", self._sync_all_now)
        menu.addAction("Synced folders…", self._open_sync_dialog)

    def _open_sync_dialog(self) -> None:
        dialog = self._dialogs.get("sync")
        if isinstance(dialog, SyncFoldersDialog):
            try:
                self._refresh_sync_dialog()
                self._present(dialog)
                return
            except RuntimeError:
                self._dialogs.pop("sync", None)  # its C++ side has gone
        dialog = SyncFoldersDialog(self._profile.label, self)
        dialog.sync_now.connect(lambda rid: self._sync_now(self._rule(rid)))
        dialog.mode_changed.connect(self._on_sync_mode_changed)
        dialog.removals_changed.connect(self._on_sync_removals_changed)
        dialog.scope_changed.connect(self._on_sync_scope_changed)
        dialog.removed.connect(self._on_sync_forget)
        self._dialogs["sync"] = dialog
        self._refresh_sync_dialog()
        self._present(dialog)
        self._set_status(f"Synced folders: {len(self._sync_rules())} rule(s).")

    def _present(self, dialog) -> None:
        """Show a window opened from a menu, and put it where it can be seen.

        A dialog shown while a popup menu is still closing loses the race with
        Windows re-activating the main window, and a dialog parented to a widget
        has no taskbar button of its own - so it ends up behind everything with
        no way to find it, which looks exactly like nothing having happened.
        Raising it on the next turn of the event loop is what fixes that.
        """
        dialog.show()

        def front() -> None:
            try:
                dialog.raise_()
                dialog.activateWindow()
            except RuntimeError:
                pass  # closed again before the event loop got here

        QTimer.singleShot(0, front)

    def _refresh_sync_dialog(self) -> None:
        dialog = self._dialogs.get("sync")
        if isinstance(dialog, SyncFoldersDialog):
            dialog.set_rules(self._sync_rules(), active=self._armed_rules())

    # ----- commands -------------------------------------------------------
    def _set_active(self, *, remote: bool) -> None:
        self._remote_active = remote

    def _active_pane(self) -> _FilePane:
        return self._remote if self._remote_active else self._local

    def _on_browse_local(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a local directory", self._local.path
        )
        if chosen:
            self._load_local(chosen)

    def _on_local_drop(self, payload: object) -> None:
        """Files dropped on the local pane from outside are copied in."""
        paths, target = _dropped(payload, self._local.path)
        if not paths:
            return
        for path in paths:
            try:
                if os.path.isdir(path):
                    shutil.copytree(
                        path, os.path.join(target, os.path.basename(path)),
                        dirs_exist_ok=True,
                    )
                else:
                    shutil.copy2(path, target)
            except OSError as exc:
                self._set_status(f"{path}: {exc}")
        self._load_local(target)

    def _on_remote_drop(self, payload: object) -> None:
        """Files dropped on the remote pane from outside are uploaded."""
        paths, target = _dropped(payload, self._remote.path or "/")
        if not paths or not self._require_connection():
            return
        items = [(path, os.path.isdir(path)) for path in paths if os.path.exists(path)]
        if not items:
            return
        if not self._confirm_production(f"upload {len(items)} item(s) to"):
            return
        self._upload_requested.emit((items, self._ignore_rules()), target)

    def _on_pane_drop(self, payload: object) -> None:
        """Rows dragged from one pane and dropped on the other."""
        if not isinstance(payload, dict):
            return
        base = str(payload.get("base") or "")
        target = str(payload.get("target") or "")
        from_remote = bool(payload.get("remote"))
        items: list[tuple[str, bool]] = []
        for entry in payload.get("items", []):
            try:
                name, is_dir = str(entry[0]), bool(entry[1])
            except (IndexError, TypeError, ValueError):
                continue
            source = RemoteFS.join(base, name) if from_remote else os.path.join(base, name)
            items.append((source, is_dir))
        if not items or not target or not self._require_connection():
            return
        if from_remote:
            os.makedirs(target, exist_ok=True)
            self._download_requested.emit((items, self._ignore_rules()), target)
            self._set_status(f"Downloading {len(items)} item(s) to {target}.")
            return
        if not self._confirm_production(f"upload {len(items)} item(s) to"):
            return
        self._upload_requested.emit((items, self._ignore_rules()), target)
        self._set_status(f"Uploading {len(items)} item(s) to {target}.")

    def _on_upload(self) -> None:
        if not self._require_connection():
            return
        selection = self._local.selection()
        if not selection:
            self._set_status("Select something in the local pane to upload.")
            return
        if not self._confirm_production(f"upload {len(selection)} item(s) to"):
            return
        items = [(self._local_child(name), is_dir) for name, is_dir in selection]
        self._upload_requested.emit((items, self._ignore_rules()), self._remote.path or "/")

    def _on_download(self) -> None:
        if not self._require_connection():
            return
        selection = self._remote.selection()
        if not selection:
            self._set_status("Select something in the remote pane to download.")
            return
        items = [(self._remote_child(name), is_dir) for name, is_dir in selection]
        self._download_requested.emit((items, self._ignore_rules()), self._local.path)

    def _on_mkdir(self) -> None:
        side = "remote" if self._remote_active else "local"
        name, ok = QInputDialog.getText(
            self, "New folder", f"Name of the new {side} folder:"
        )
        name = name.strip()
        if not ok or not name:
            return
        if self._remote_active:
            if not self._require_connection():
                return
            self._mkdir_requested.emit(self._remote_child(name))
        else:
            try:
                os.mkdir(self._local_child(name))
            except OSError as exc:
                QMessageBox.warning(self, "Could not create folder", str(exc))
                return
            self._load_local(self._local.path)

    def _on_rename(self) -> None:
        pane = self._active_pane()
        selection = pane.selection()
        if len(selection) != 1:
            self._set_status("Select exactly one entry to rename.")
            return
        old_name, _is_dir = selection[0]
        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=old_name
        )
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        if self._remote_active:
            if not self._require_connection():
                return
            self._rename_requested.emit(
                self._remote_child(old_name), self._remote_child(new_name)
            )
        else:
            try:
                os.rename(self._local_child(old_name), self._local_child(new_name))
            except OSError as exc:
                QMessageBox.warning(self, "Could not rename", str(exc))
                return
            self._load_local(self._local.path)

    def _on_delete(self) -> None:
        pane = self._active_pane()
        selection = pane.selection()
        if not selection:
            self._set_status("Select something to delete.")
            return
        side = "remote" if self._remote_active else "local"
        names = ", ".join(name for name, _ in selection[:5])
        if len(selection) > 5:
            names += f", … ({len(selection)} total)"
        has_folder = any(is_dir for _, is_dir in selection)
        detail = f"Delete the following from the {side} side?\n\n{names}"
        if has_folder:
            detail += "\n\nFolders are deleted with everything inside them."
        if self._remote_active and not self._confirm_production("delete files on"):
            return
        confirm = QMessageBox.question(self, "Delete", detail)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self._remote_active:
            if not self._require_connection():
                return
            for name, is_dir in selection:
                self._delete_requested.emit(self._remote_child(name), is_dir)
        else:
            self._delete_local(selection)

    def _delete_local(self, selection: list[tuple[str, bool]]) -> None:
        for name, is_dir in selection:
            target = self._local_child(name)
            try:
                if is_dir:
                    shutil.rmtree(target)
                else:
                    os.unlink(target)
            except OSError as exc:
                QMessageBox.warning(self, "Could not delete", f"{name}: {exc}")
        self._load_local(self._local.path)

    def _on_cancel(self) -> None:
        # cancel() only flips a flag, so calling it across threads is safe.
        self._worker.cancel()
        self._set_status("Cancelling…")

    def _on_clear_finished(self) -> None:
        self._clear_finished_requested.emit()
        self._queue_panel.remove_finished()

    def _queue_panel_visible(self, visible: bool) -> None:
        self._queue_panel.setVisible(visible)

    # ----- permissions and links -----------------------------------------
    def _on_permissions(self) -> None:
        if not self._require_connection():
            return
        selection = self._remote.selection()
        if len(selection) != 1:
            self._set_status("Select exactly one remote entry.")
            return
        name, is_dir = selection[0]
        entry = self._remote.entry(name)
        dialog = PermissionsDialog(
            name,
            entry.mode if entry is not None else None,
            is_dir=is_dir,
            allow_recursive=Capability.EXEC in self._capabilities,
            parent=self,
        )
        if not dialog.exec():
            return
        mode = dialog.mode()
        if perm.is_risky(mode):
            confirm = QMessageBox.question(
                self,
                "Are you sure?",
                f"{perm.describe(mode, is_dir=is_dir)} is more permissive than "
                "it usually should be. Apply it anyway?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._chmod_requested.emit(
            self._remote_child(name), mode, dialog.recursive(), dialog.scope()
        )

    def _on_link_target(self) -> None:
        if not self._require_connection():
            return
        selection = self._remote.selection()
        if len(selection) != 1:
            return
        name, _is_dir = selection[0]
        entry = self._remote.entry(name)
        if entry is None or not entry.is_link:
            self._set_status(f"{name} is not a symbolic link.")
            return
        dialog = LinkTargetDialog(name, entry.link_target, self)
        if not dialog.exec():
            return
        target = dialog.target()
        if target and target != entry.link_target:
            if not self._confirm_production("change a symlink on"):
                return
            self._symlink_requested.emit(target, self._remote_child(name))

    # ----- server-side tools ---------------------------------------------
    def _on_archive(self) -> None:
        if not self._require_shell():
            return
        selection = self._remote.selection()
        if not selection:
            self._set_status("Select what to put in the archive.")
            return
        stem = selection[0][0] if len(selection) == 1 else "archive"
        dialog = ArchiveDialog(len(selection), f"{stem}.tar.gz", self)
        if not dialog.exec():
            return
        name = dialog.name()
        if not name:
            return
        self._archive_requested.emit(
            self._remote.path or "/",
            "\n".join(item[0] for item in selection),
            RemoteFS.join(self._remote.path or "/", name),
            dialog.kind(),
        )
        self._set_status(f"Packing {len(selection)} item(s) on the server…")

    def _on_extract(self) -> None:
        if not self._require_shell():
            return
        selection = self._remote.selection()
        if len(selection) != 1:
            self._set_status("Select one archive to unpack.")
            return
        name = selection[0][0]
        from mysql_runner.transfer.remote_exec import archive_kind_for

        if not archive_kind_for(name):
            self._set_status(f"{name} is not an archive this can unpack.")
            return
        target, ok = QInputDialog.getText(
            self,
            "Unpack",
            "Unpack into which directory on the server?",
            text=self._remote.path or "/",
        )
        if not ok or not target.strip():
            return
        self._extract_requested.emit(self._remote_child(name), target.strip())
        self._set_status(f"Unpacking {name} on the server…")

    def _on_search(self) -> None:
        if not self._require_shell():
            return
        dialog = self._dialogs.get("grep")
        if not isinstance(dialog, RemoteSearchDialog):
            dialog = RemoteSearchDialog(self._remote.path or "/", self)
            dialog.search_requested.connect(self._grep_requested.emit)
            dialog.open_requested.connect(self._on_open_search_hit)
            self._dialogs["grep"] = dialog
        dialog.show()
        dialog.raise_()

    def _show_grep(self, payload: object) -> None:
        dialog = self._dialogs.get("grep")
        if isinstance(dialog, RemoteSearchDialog):
            dialog.show_results(payload)

    def _on_open_search_hit(self, path: str, _line: int) -> None:
        self._list_remote(RemoteFS.parent(path))

    def _on_disk_usage(self) -> None:
        if not self._require_shell():
            return
        dialog = self._dialogs.get("disk_usage")
        if not isinstance(dialog, DiskUsageDialog):
            dialog = DiskUsageDialog(self._remote.path or "/", self)
            dialog.usage_requested.connect(self._disk_usage_requested.emit)
            dialog.open_requested.connect(self._list_remote)
            self._dialogs["disk_usage"] = dialog
        dialog.show()
        dialog.raise_()
        dialog.start()

    def _show_disk_usage(self, payload: object) -> None:
        dialog = self._dialogs.get("disk_usage")
        if isinstance(dialog, DiskUsageDialog):
            dialog.show_usage(payload)

    def _on_logs(self) -> None:
        if not self._require_shell():
            return
        self._logs_requested.emit(self._remote.path or "/")
        self._set_status("Looking for log files…")

    def _show_logs(self, payload: object) -> None:
        candidates = payload if isinstance(payload, list) else []
        first = candidates[0] if candidates else ""
        dialog = LogViewerDialog(self._spec, first, candidates=candidates, parent=self)
        self._dialogs["logs"] = dialog
        dialog.show()

    def _on_command_bar(self) -> None:
        if not self._require_shell():
            return
        dialog = self._dialogs.get("exec")
        if not isinstance(dialog, CommandBar):
            dialog = CommandBar(
                self._remote.path or "/", history=self._command_history, parent=self
            )
            dialog.command_requested.connect(self._on_run_command)
            self._dialogs["exec"] = dialog
        dialog.show()
        dialog.raise_()

    def _on_run_command(self, command: str, cwd: str) -> None:
        if self._is_production and self._settings.production_guard:
            if not self._confirm_production(f"run “{command}” on"):
                return
        self._exec_requested.emit(command, cwd)

    def _show_exec(self, payload: object) -> None:
        dialog = self._dialogs.get("exec")
        if isinstance(dialog, CommandBar):
            dialog.show_result(payload)
            self._command_history = dialog.history()
        elif payload is not None:
            self._set_status(getattr(payload, "stdout", "").strip()[:400])

    def _on_snippets(self) -> None:
        selection = self._remote.selection()
        context = {
            "remote_dir": self._remote.path or "/",
            "local_dir": self._local.path,
            "file": selection[0][0] if selection else "",
            "path": self._remote_child(selection[0][0]) if selection else "",
            "host": self._profile.host,
            "user": self._profile.username,
        }
        dialog = SnippetsDialog(
            self._snippets,
            context,
            can_run=Capability.EXEC in self._capabilities,
            parent=self,
        )
        dialog.run_requested.connect(
            lambda command: self._on_run_command(command, self._remote.path or "/")
        )
        self._dialogs["snippets"] = dialog
        dialog.show()

    def _on_terminal(self) -> None:
        if not self._require_shell():
            return
        self.shell_requested.emit(self._profile, self._spec, self._remote.path or "")

    def _on_external_terminal(self) -> None:
        terminals = spawn.detect_terminals()
        if not terminals:
            QMessageBox.information(
                self,
                "No terminal found",
                "PuTTY, Windows Terminal, ssh.exe and WSL were all looked for "
                "and none of them is installed.",
            )
            return
        wanted = self._settings.terminal_program
        chosen = next((t for t in terminals if t.name == wanted), terminals[0])
        target = spawn.ShellTarget(
            host=self._profile.host,
            port=self._profile.effective_port,
            username=self._profile.username,
            password=self._profile.password,
            key_path=self._profile.private_key_path,
            remote_dir=self._remote.path or "",
        )
        try:
            spawn.launch(
                chosen,
                target,
                include_password=self._settings.terminal_send_password,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Could not start the terminal", str(exc))
            return
        self._set_status(f"Opened {chosen.name} in {target.remote_dir or 'the home directory'}.")

    # ----- history / undo -------------------------------------------------
    def _on_history(self) -> None:
        dialog = HistoryDialog(
            self._history_store,
            profile_id=self._profile.id,
            profile_label=self._profile.label,
            parent=self,
        )
        dialog.undo_requested.connect(self._on_undo_entry)
        self._dialogs["history"] = dialog
        dialog.show()

    def _on_undo_entry(self, entry_id: str) -> None:
        # The journal is re-read when the worker reports back; see
        # _on_op_message.
        self._undo_requested.emit(entry_id)

    def _on_undo_last(self) -> None:
        entry = self._history_store.latest_undoable(profile_id=self._profile.id)
        if entry is None:
            self._set_status("Nothing this app overwrote is still recoverable.")
            return
        confirm = QMessageBox.question(
            self,
            "Undo the last replace?",
            f"Put the previous version of {entry.name} back?\n\n{entry.target}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._undo_requested.emit(entry.id)
        self._sync_undo_button()

    def _sync_undo_button(self) -> None:
        available = self._history_store.latest_undoable(profile_id=self._profile.id)
        self._undo_btn.setEnabled(available is not None)
        self._undo_btn.setToolTip(
            f"Restore {available.name}" if available is not None
            else "Nothing to undo yet"
        )

    def _show_digest(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        path = payload.get("path", "")
        digest = payload.get("digest", "")
        if not digest:
            self._set_status(f"Could not hash {path}.")
            return
        local_note = ""
        name = RemoteFS.basename(path)
        candidate = os.path.join(self._local.path, name)
        if os.path.isfile(candidate):
            from mysql_runner.transfer.hashing import hash_local_file

            try:
                local_digest = hash_local_file(candidate)
            except Exception:
                local_digest = ""
            if local_digest:
                same = "identical to" if local_digest == digest else "different from"
                local_note = f"\n\nThe local {name} is {same} this."
        QMessageBox.information(
            self, f"Digest — {name}", f"sha256\n{digest}{local_note}"
        )

    # ----- context menus -------------------------------------------------
    def _show_menu(self, position, *, remote: bool) -> None:
        pane = self._remote if remote else self._local
        selection = pane.selection()
        menu = QMenu(self)
        has_shell = Capability.EXEC in self._capabilities

        if remote:
            menu.addAction("Download", self._on_download).setEnabled(bool(selection))
            menu.addSeparator()
        else:
            menu.addAction("Upload", self._on_upload).setEnabled(bool(selection))
            menu.addSeparator()

        menu.addAction("New folder", self._on_mkdir)
        menu.addAction("Rename", self._on_rename).setEnabled(len(selection) == 1)
        menu.addAction("Delete", self._on_delete).setEnabled(bool(selection))
        menu.addAction("Refresh", pane.refresh)
        menu.addSeparator()

        if remote:
            menu.addAction("Permissions…", self._on_permissions).setEnabled(
                len(selection) == 1 and Capability.CHMOD in self._capabilities
            )
            entry = pane.entry(selection[0][0]) if len(selection) == 1 else None
            menu.addAction("Link target…", self._on_link_target).setEnabled(
                entry is not None and entry.is_link
            )
            menu.addAction("Digest…", self._on_digest_selected).setEnabled(
                len(selection) == 1 and not selection[0][1]
            )
            archive = menu.addAction("Archive on the server…", self._on_archive)
            archive.setEnabled(bool(selection) and has_shell)
            extract = menu.addAction("Unpack here…", self._on_extract)
            extract.setEnabled(len(selection) == 1 and has_shell)
            menu.addSeparator()
            menu.addAction("Search here…", self._on_search).setEnabled(has_shell)
            menu.addAction("Disk usage here…", self._on_disk_usage).setEnabled(has_shell)
            menu.addAction("Open a shell here", self._on_terminal).setEnabled(has_shell)
            menu.addAction("Copy remote path", self._copy_remote_path)
        else:
            folder = self._selected_local_dir()
            synced = self._sync_store.find(self._profile.id, folder)
            sync_menu = menu.addMenu(
                "Sync folder" if synced is None else f"Sync folder ({synced.mode.label})"
            )
            self._fill_sync_menu(sync_menu)
            menu.addSeparator()
            menu.addAction("Open in Explorer", self._open_in_explorer)
            menu.addAction("Copy path", self._copy_local_path)
            menu.addSeparator()
            menu.addAction("Compare with the server", lambda: self._on_compare())

        menu.exec(position)

    def _on_digest_selected(self) -> None:
        selection = self._remote.selection()
        if len(selection) != 1:
            return
        self._tool_progress.start("Hashing…")
        self._digest_requested.emit(self._remote_child(selection[0][0]))

    def _copy_remote_path(self) -> None:
        selection = self._remote.selection()
        path = (
            self._remote_child(selection[0][0]) if selection else self._remote.path
        )
        self._copy_to_clipboard(path)

    def _copy_local_path(self) -> None:
        selection = self._local.selection()
        path = self._local_child(selection[0][0]) if selection else self._local.path
        self._copy_to_clipboard(path)

    def _copy_to_clipboard(self, text: str) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
            self._set_status(f"Copied {text}")

    def _open_in_explorer(self) -> None:
        selection = self._local.selection()
        target = (
            self._local_child(selection[0][0]) if selection else self._local.path
        )
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ----- guards ---------------------------------------------------------
    def _require_connection(self) -> bool:
        if self._connected:
            return True
        self._set_status("Not connected.")
        return False

    def _require_shell(self) -> bool:
        if not self._require_connection():
            return False
        if Capability.EXEC in self._capabilities:
            return True
        QMessageBox.information(
            self,
            "Not available on this connection",
            "This is a file-transfer-only protocol, so there is no shell to run "
            "anything in. Connect over SFTP for the server-side tools.",
        )
        return False

    def _confirm_production(self, action: str) -> bool:
        """Ask before anything destructive on a production connection."""
        if not self._is_production or not self._settings.production_guard:
            return True
        confirm = QMessageBox.warning(
            self,
            "PRODUCTION",
            f"You are about to {action} {self._profile.label}, which is marked "
            "as production.\n\nGo ahead?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return confirm == QMessageBox.StandardButton.Yes

    # ----- status ---------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self._status.setText(message)

    # ----- teardown -------------------------------------------------------
    def cleanup(self) -> None:
        """Cancel any transfer, close the connection, stop the thread."""
        self._closing = True
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        for rule_id in list(self._sync_watchers) + list(self._git_watchers):
            self._stop_sync_rule(rule_id)
        for dialog in list(self._dialogs.values()):
            try:
                dialog.close()
            except RuntimeError:
                pass
        self._dialogs.clear()
        self._worker.cancel()
        self._worker.cancel_tools()
        # Stop the worker talking back before this widget starts tearing down.
        # A connection attempt still in flight would otherwise deliver its
        # result to a half-deleted tab.
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


def _scan_local(target: str) -> list[RemoteEntry]:
    """One local directory, in the same shape as a remote listing."""
    entries: list[RemoteEntry] = []
    with os.scandir(target) as scan:
        for item in scan:
            try:
                stat_result = item.stat()
                size = stat_result.st_size
                modified = stat_result.st_mtime
            except OSError:
                size, modified = 0, None
            entries.append(
                RemoteEntry(
                    name=item.name,
                    is_dir=item.is_dir(),
                    size=size,
                    modified=modified,
                    is_link=item.is_symlink(),
                )
            )
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def _spec_for(profile: ServerProfile) -> ConnectionSpec:
    return ConnectionSpec(
        kind=profile.kind,
        host=profile.host,
        port=profile.effective_port,
        username=profile.username,
        password=profile.password,
        private_key_path=profile.private_key_path,
        passive=profile.passive,
    )


def _menu_button(text: str, tip: str, entries) -> QToolButton:
    """A button that drops a menu - how a row of seven buttons becomes one.

    ``entries`` is a sequence of (label, shortcut, callback); a None label
    inserts a separator.
    """
    button = QToolButton()
    button.setObjectName("menubutton")
    button.setText(f"{text}  ▾")
    button.setToolTip(tip)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(button)
    for label, shortcut, callback in entries:
        if label is None:
            menu.addSeparator()
            continue
        action = menu.addAction(label, callback)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            # The tab owns the real shortcuts; this only shows them in the menu.
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
    button.setMenu(menu)
    return button


def _icon_button(kind: str, tip: str) -> QToolButton:
    """A small navigation button with a painted, theme-coloured glyph."""
    button = QToolButton()
    button.setIcon(theme.nav_icon(kind, True))
    button.setIconSize(QSize(14, 14))
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.setFixedSize(24, 22)
    return button


def _inside(child: str, parent: str) -> bool:
    """Whether ``child`` sits below ``parent`` (case-insensitively)."""
    if not child or not parent:
        return False
    mine = os.path.normcase(os.path.normpath(parent)).rstrip("\\/")
    theirs = os.path.normcase(os.path.normpath(child)).rstrip("\\/")
    return theirs.startswith(mine + os.sep)


def _dropped(payload: object, fallback: str) -> tuple[list[str], str]:
    """Unpack a paths_dropped payload: (local paths, directory to drop into)."""
    if isinstance(payload, dict):
        paths = [str(path) for path in payload.get("paths", [])]
        return paths, str(payload.get("target") or fallback)
    if isinstance(payload, list):  # older callers passed a bare list
        return [str(path) for path in payload], fallback
    return [], fallback


def _decode_rows(mime) -> dict | None:
    """The payload of a pane-to-pane drag, or None when this is not one."""
    if not mime.hasFormat(_ROWS_MIME):
        return None
    try:
        payload = json.loads(bytes(mime.data(_ROWS_MIME)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("items"):
        return None
    return payload


def _hover_colour(dark: bool) -> QColor:
    """The tint used on the folder a drag is hovering over."""
    colour = QColor(theme.palette(dark).accent)
    colour.setAlpha(70)
    return colour


def _display_name(entry: RemoteEntry) -> str:
    """How one entry is shown: folders bracketed, links with their target."""
    label = f"[{entry.name}]" if entry.is_dir else entry.name
    if entry.is_link:
        label = f"{label} →" + (f" {entry.link_target}" if entry.link_target else "")
    return label


# ----- formatting helpers -------------------------------------------------
def _human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _human_time(epoch: float | None) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""
