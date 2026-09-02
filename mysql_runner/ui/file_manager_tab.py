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
import tempfile
import threading
import uuid
from datetime import datetime

from PyQt6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QSize,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
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

from mysql_runner.runtime_mode import mapped_drive_letter, running_elevated
from mysql_runner.storage.models import (
    ConnectionKind,
    Environment,
    ServerProfile,
)
from mysql_runner.storage.settings import Settings
from mysql_runner.transfer import editors
from mysql_runner.transfer import permissions as perm
from mysql_runner.transfer import spawn
from mysql_runner.transfer.base import (
    Capability,
    RemoteEntry,
    RemoteFS,
    local_relative,
)
from mysql_runner.transfer.githistory import Export, commit_subject, export_files
from mysql_runner.transfer.gitwatch import (
    CommitEvent,
    GitCommitWatcher,
    commit_changes,
    describe_repo,
    find_repo,
)
from mysql_runner.transfer.hashing import DiffStatus
from mysql_runner.transfer import hostkeys
from mysql_runner.transfer.history import HistoryStore
from mysql_runner.transfer.shellhistory import ShellHistory
from mysql_runner.transfer.ignore import (
    IgnoreRules,
    add_patterns,
    ignore_file_path,
    pattern_for,
)
from mysql_runner.transfer.navhistory import NavHistory, mirror_path
from mysql_runner.transfer.pool import JobState, Overwrite, PoolOptions
from mysql_runner.transfer.snippets import SnippetLibrary
from mysql_runner.transfer.syncrules import (
    SyncMode,
    SyncRule,
    SyncRuleStore,
    normalise_remote,
)
from mysql_runner.transfer.treestat import FolderStatsCache, local_folder_stats
from mysql_runner.transfer.watcher import Change, ChangeKind, DirectoryWatcher, summarise
from mysql_runner.transfer.worker import ConnectionSpec, TransferWorker
from mysql_runner.ui import theme
from mysql_runner.ui.commit_plan_dialog import CommitPlanDialog
from mysql_runner.ui.compare_dialog import CompareDialog
from mysql_runner.ui.git_history_dialog import GitHistoryDialog
from mysql_runner.ui.history_dialog import HistoryDialog
from mysql_runner.ui.sync_activity_dialog import SyncActivityDialog
from mysql_runner.ui.log_viewer import LogViewerDialog
from mysql_runner.ui.permissions_dialog import PermissionsDialog
from mysql_runner.ui.remote_folder_dialog import RemoteFolderDialog
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
from mysql_runner.ui.changes_panel import ChangesPanel
from mysql_runner.ui.transfer_queue_panel import TransferQueuePanel, origin_with_note

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

#: Editing a remote file bigger than this asks first - the whole file comes
#: down now and goes back up on every save.
_MAX_EDIT_BYTES = 20 * 1024 * 1024

#: Shown on the greyed-out "Open in VS Code" entries, because a disabled item
#: that says nothing is a dead end.
_NO_EDITOR_HINT = (
    "No VS Code on this machine - or its command line is not on PATH. "
    "Visual Studio Code, Insiders, Cursor, VSCodium and Windsurf are all "
    "looked for."
)

#: How close to a pane's top or bottom edge a drag has to be before the
#: listing starts scrolling itself, and how often it steps while it is there.
_DRAG_EDGE = 32
_DRAG_SCROLL_MS = 60

#: Extension -> icon flavour. The icons are painted (see theme.entry_icon), so
#: remote files - which have no real path for the native icon provider to look
#: at - get exactly the same glyphs as local ones.
_CODE_EXT = frozenset((
    ".php", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".scss", ".less", ".sql", ".json", ".xml", ".yml", ".yaml",
    ".sh", ".ps1", ".bat", ".ini", ".conf", ".env", ".htaccess", ".twig",
    ".vue", ".c", ".h", ".cpp", ".cs", ".java", ".rb", ".go", ".rs", ".pl",
    ".toml", ".md", ".lock",
))
_IMAGE_EXT = frozenset((
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".tif", ".tiff", ".avif",
))
_ARCHIVE_EXT = frozenset(
    (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".rar", ".7z")
)


def _icon_kind(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = os.path.splitext(name)[1].lower()
    if ext in _CODE_EXT:
        return "file-code"
    if ext in _IMAGE_EXT:
        return "file-image"
    if ext in _ARCHIVE_EXT:
        return "file-archive"
    return "file"


class _FileTable(QTableWidget):
    """Listing table that reports when it takes focus, and starts drags."""

    focused = pyqtSignal()
    delete_pressed = pyqtSignal()
    #: The mouse's own Back / Forward buttons, pressed over this pane.
    back_requested = pyqtSignal()
    forward_requested = pyqtSignal()

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        # The pane installs this: it knows which side it is and what is picked.
        self.drag_payload = lambda: None

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.focused.emit()
        super().focusInEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # The thumb buttons navigate, the way every browser and Explorer do.
        if event.button() == Qt.MouseButton.BackButton:
            self.back_requested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.ForwardButton:
            self.forward_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Handled here rather than as a window shortcut, so pressing Delete
        # while typing in a filter or path box can never mean "delete files".
        if event.key() == Qt.Key.Key_Delete:
            self.delete_pressed.emit()
            return
        super().keyPressEvent(event)

    def startDrag(self, _actions) -> None:  # noqa: N802 - Qt naming
        """Begin dragging the selected rows out of this pane."""
        data = self.drag_payload()
        if data is None:
            return
        drag = QDrag(self)
        drag.setMimeData(data)
        drag.exec(Qt.DropAction.CopyAction)


class _NoticeBar(QWidget):
    """A dismissible strip above the panes that says something and offers actions.

    Deliberately not a QMessageBox: what appears here is noticed in the
    background - a commit, most of all - and background news must never seize
    the window. A modal box stole focus mid-keystroke and had to be answered
    before anything else could happen, which is exactly the wrong shape for
    "by the way, want me to push that?". This waits instead, and the offer
    stays in the Sync menu even after it is dismissed.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notice")
        # A QWidget subclass paints no stylesheet background or border without
        # this - the strip would be an invisible row of loose buttons.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setVisible(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 6, 6)
        row.setSpacing(8)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setObjectName("noticetext")
        # The buttons answer the offer; clicking the sentence asks what the
        # offer actually is, which for anything involving a file list is the
        # question that gets asked first.
        self._label.mousePressEvent = (  # type: ignore[method-assign]
            lambda _event: self._click()
        )
        row.addWidget(self._label, 1)
        self._remember = QCheckBox("")
        self._remember.setVisible(False)
        row.addWidget(self._remember)
        #: Action buttons, rebuilt per notice.
        self._buttons: list[QPushButton] = []
        self._row = row
        self._on_dismiss = None
        self._on_click = None
        close = QToolButton()
        close.setObjectName("tabclose")
        close.setText("✕")
        close.setToolTip("Dismiss")
        close.setAutoRaise(True)
        close.setFixedSize(18, 18)
        close.clicked.connect(self.dismiss)
        self._close = close
        row.addWidget(close)

    def show_notice(
        self,
        text: str,
        actions: list[tuple[str, object]],
        *,
        detail: str = "",
        checkbox: str = "",
        on_dismiss=None,
        on_click=None,
    ) -> None:
        """Say ``text`` and offer ``actions`` as (label, callback) pairs.

        A callback runs on the GUI thread when its button is pressed; the bar
        hides itself first, so a callback that opens something of its own is
        never drawn behind this strip. ``on_dismiss`` is called when the strip
        is closed instead - which is how a ticked checkbox is honoured by
        someone who answers by walking away. ``on_click`` runs when the text
        itself is clicked, for a notice that has more to say than fits.
        """
        self._on_dismiss = on_dismiss
        self._on_click = on_click
        self._clear_buttons()
        self._label.setText(text)
        self._label.setCursor(
            Qt.CursorShape.PointingHandCursor
            if on_click is not None
            else Qt.CursorShape.ArrowCursor
        )
        hint = "Click for the whole list." if on_click is not None else ""
        self._label.setToolTip(
            "\n\n".join(part for part in (detail, hint) if part)
        )
        self._remember.setVisible(bool(checkbox))
        self._remember.setText(checkbox)
        self._remember.setChecked(False)
        for index, (label, callback) in enumerate(actions):
            button = QPushButton(label)
            if index == 0:
                button.setObjectName("primary")
            button.clicked.connect(
                lambda _checked=False, run=callback: self._run(run)
            )
            # Before the checkbox and the close button, after the text.
            self._row.insertWidget(1 + index, button)
            self._buttons.append(button)
        self.setVisible(True)

    def remembered(self) -> bool:
        """Whether the notice's checkbox was ticked."""
        return self._remember.isChecked()

    def dismiss(self) -> None:
        self.setVisible(False)
        callback, self._on_dismiss = self._on_dismiss, None
        self._on_click = None
        self._clear_buttons()
        self._label.setText("")
        if callback is not None:
            callback()

    def hide_quietly(self) -> None:
        """Take the strip down without counting it as an answer.

        For when what the notice said stops being true rather than the user
        having closed it - the folder it named moved out from under it, say.
        Firing the dismissal callback there would record an answer nobody gave.
        """
        self._on_dismiss = None
        self._on_click = None
        self.setVisible(False)
        self._clear_buttons()
        self._label.setText("")

    def _click(self) -> None:
        if self._on_click is not None:
            self._on_click()

    def _run(self, callback) -> None:
        self.setVisible(False)
        self._on_dismiss = None  # answered; the dismissal path is not owed one
        self._on_click = None
        try:
            callback()
        finally:
            self._clear_buttons()

    def _clear_buttons(self) -> None:
        for button in self._buttons:
            self._row.removeWidget(button)
            button.deleteLater()
        self._buttons.clear()


class _PathBar(QWidget):
    """The current path as clickable crumbs, with the plain edit a click away.

    Each segment is a button that jumps straight back to that folder - being
    three levels deep and wanting the first one back is the single most common
    navigation there is. Clicking the empty space to the right of the crumbs
    turns the bar into the familiar line edit for typing or pasting a path;
    Enter navigates, Escape or clicking elsewhere puts the crumbs back.
    """

    navigate = pyqtSignal(str)
    #: The Browse button was pressed; the pane decides what browsing means.
    browse = pyqtSignal()

    #: More segments than this collapse into one "…" menu after the root, so
    #: a deep path cannot push the folder you are in out of view.
    _MAX_CRUMBS = 7

    def __init__(self, *, posix: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._posix = posix
        self._path = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Path")
        self._edit.returnPressed.connect(self._commit_edit)
        self._edit.installEventFilter(self)

        self._crumbs = QWidget()
        self._crumbs.setObjectName("pathbar")
        self._crumbs.setToolTip(
            "Click a folder to go back to it; click the empty space to type a path"
        )
        self._crumbs.setFixedHeight(self._edit.sizeHint().height())
        self._crumbs.mousePressEvent = (  # type: ignore[method-assign]
            lambda _event: self._start_edit()
        )
        self._row = QHBoxLayout(self._crumbs)
        self._row.setContentsMargins(4, 0, 4, 0)
        self._row.setSpacing(0)

        # Typing a path means knowing it; a picker is for the far commoner
        # case of knowing the folder when you see it. Local opens Windows's own
        # dialog, remote a tree of the server - see FileManagerTab.
        self._browse = _icon_button(
            "browse",
            "Browse the server for a folder…"
            if posix
            else "Browse for a local folder…",
        )
        self._browse.clicked.connect(self.browse.emit)

        layout.addWidget(self._crumbs)
        layout.addWidget(self._edit)
        layout.addWidget(self._browse)
        self._show_crumbs()

    def mark_side(self, *, remote: bool, live: bool = False) -> None:
        """Tint the crumb strip by which side of the transfer this is.

        The strip carrying ``#pathbar`` is the one inside the bar, not the bar
        itself, so the property has to go on that or the rule matches nothing.
        """
        strip = self._crumbs
        strip.setProperty("side", "remote" if remote else "local")
        strip.setProperty("live", "true" if live else "false")
        strip.style().unpolish(strip)
        strip.style().polish(strip)

    # ----- state ------------------------------------------------------------
    def set_path(self, path: str) -> None:
        self._path = path
        # A listing arriving while the edit is open means the answer came back;
        # showing it as crumbs is the confirmation.
        self._show_crumbs()

    def _show_crumbs(self) -> None:
        if not self._path:
            # Nowhere to crumb to yet: typing is the only way in.
            self._crumbs.setVisible(False)
            self._edit.setVisible(True)
            return
        self._edit.setVisible(False)
        self._rebuild()
        self._crumbs.setVisible(True)

    # ----- edit mode ----------------------------------------------------------
    def _start_edit(self) -> None:
        if not self.isEnabled():
            return
        self._crumbs.setVisible(False)
        self._edit.setText(self._path)
        self._edit.setVisible(True)
        self._edit.setFocus()
        self._edit.selectAll()

    def _commit_edit(self) -> None:
        text = self._edit.text().strip()
        self._show_crumbs()
        if text:
            self.navigate.emit(text)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if obj is self._edit and self._path:
            if (
                event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
            ):
                self._show_crumbs()
                return True
            if event.type() == QEvent.Type.FocusOut:
                self._show_crumbs()
        return super().eventFilter(obj, event)

    # ----- building the crumbs ------------------------------------------------
    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        segments = self._segments()
        hidden: list[tuple[str, str]] = []
        if len(segments) > self._MAX_CRUMBS:
            keep_tail = self._MAX_CRUMBS - 2
            hidden = segments[1:-keep_tail]
            segments = [segments[0]] + segments[-keep_tail:]
        for position, (label, target) in enumerate(segments):
            if position == 1 and hidden:
                self._row.addWidget(self._separator())
                self._row.addWidget(self._overflow_button(hidden))
            if position:
                self._row.addWidget(self._separator())
            self._row.addWidget(self._crumb_button(label, target))
        self._row.addStretch(1)

    def _crumb_button(self, label: str, target: str) -> QToolButton:
        button = QToolButton()
        shown = button.fontMetrics().elidedText(
            label, Qt.TextElideMode.ElideMiddle, 200
        )
        button.setText(shown)
        button.setToolTip(target)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False, t=target: self.navigate.emit(t))
        return button

    def _overflow_button(self, hidden: list[tuple[str, str]]) -> QToolButton:
        button = QToolButton()
        button.setText("…")
        button.setToolTip("Folders in between")
        button.setAutoRaise(True)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        for label, target in hidden:
            menu.addAction(label, lambda t=target: self.navigate.emit(t))
        button.setMenu(menu)
        return button

    @staticmethod
    def _separator() -> QLabel:
        return QLabel("›")

    def _segments(self) -> list[tuple[str, str]]:
        """(label, full path) per crumb, root first."""
        path = self._path
        if not path:
            return []
        if self._posix:
            clean = path.rstrip("/") or "/"
            crumbs = [("/", "/")]
            current = ""
            for part in clean.lstrip("/").split("/"):
                if not part:
                    continue
                current = f"{current}/{part}"
                crumbs.append((part, current))
            return crumbs
        # A UNC share's \\server\share is the root, not two folders.
        drive, tail = os.path.splitdrive(path)
        root = drive + os.sep if drive else os.sep
        crumbs = [(drive or os.sep, root)]
        current = root
        for part in tail.strip("\\/").split(os.sep):
            if not part:
                continue
            current = os.path.join(current, part)
            crumbs.append((part, current))
        return crumbs


class _FilePane(QWidget):
    """A path bar plus a listing table, used for both the local and remote side."""

    #: The user asked to show a different directory.
    navigate = pyqtSignal(str)
    #: The path bar's Browse button was pressed.
    browse = pyqtSignal()
    #: A file row was double-clicked (name only; the owner knows the side).
    open_file = pyqtSignal(str)
    #: Delete was pressed while this pane's table had focus.
    delete_requested = pyqtSignal()
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
    #: The listing was re-sorted by hand: (column, descending). Sorting by
    #: Modified is worth a folder-statistics pass the listing did not pay for
    #: on its own, so the owner hears about it.
    sort_changed = pyqtSignal(int, bool)
    #: Rows were selected or deselected. The transfer buttons say how many
    #: items they are about to move and where to, so they need to hear this.
    selection_changed = pyqtSignal()

    def __init__(self, title: str, parent: QWidget | None = None, *, posix: bool = False) -> None:
        super().__init__(parent)
        self._path = ""
        self._entries: list[RemoteEntry] = []
        self._posix = posix
        self._history = NavHistory()
        self._replaying = False
        self._dark = False
        # Newest first by default: on a live site, "what changed?" is the
        # question a listing answers far more often than "what is here?".
        self._sort_column = _MODIFIED
        self._sort_desc = True
        self._filter = ""
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
        self._count = QLabel("")
        self._count.setObjectName("hint")
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
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter")
        self._filter_edit.setToolTip(
            "Show only names containing this (Ctrl+F; Esc clears)"
        )
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setMaximumWidth(130)
        self._filter_edit.textChanged.connect(self._on_filter)
        self._filter_edit.installEventFilter(self)
        header.addWidget(self._title)
        header.addWidget(self._count)
        header.addStretch(1)
        header.addWidget(self._filter_edit)
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

        self._path_bar = _PathBar(posix=posix)
        self._path_bar.navigate.connect(self.navigate.emit)
        self._path_bar.browse.connect(self.browse.emit)
        layout.addWidget(self._path_bar)

        self._table = _FileTable(0, len(_COLUMNS))
        self._table.focused.connect(self.focused.emit)
        self._table.delete_pressed.connect(self.delete_requested.emit)
        self._table.back_requested.connect(self._on_mouse_back)
        self._table.forward_requested.connect(self.go_forward)
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
        # Qt's own sorting cannot be used: it sorts the text ("1.2 MB"), and
        # the ".." row must stay on top. _sorted() does it properly instead.
        self._table.setSortingEnabled(False)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setDefaultSectionSize(25)
        self._table.setIconSize(QSize(16, 16))
        header = self._table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setHighlightSections(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(_MODIFIED, Qt.SortOrder.DescendingOrder)
        header.sectionClicked.connect(self._on_sort)
        # The name takes whatever is left; the rest hold fixed, draggable
        # widths sized for their content ("999.9 MB", "2026-08-26 14:03").
        # Nothing re-fits them later, so a width the user drags out stays.
        header.setSectionResizeMode(_NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in (
            (_SIZE, 90), (_MODIFIED, 135), (_MODE, 60), (_SYNC, 46)
        ):
            header.resizeSection(column, width)
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
        self._table.itemSelectionChanged.connect(self._update_count)
        layout.addWidget(self._table, 1)

        self._sync_buttons()

    # ----- content --------------------------------------------------------
    def set_listing(self, path: str, entries: list[RemoteEntry], *, at_root: bool) -> None:
        if path != self._path:
            if self._filter:
                # The filter belongs to the directory it was typed in.
                self._filter_edit.blockSignals(True)
                self._filter_edit.clear()
                self._filter_edit.blockSignals(False)
                self._filter = ""
            # Going somewhere else: what was picked in the old directory
            # means nothing here, so the selection does not travel. Only
            # a re-listing of the *same* directory keeps it (see _render).
            self._table.clearSelection()
        self._path = path
        self._entries = list(entries)
        self._path_bar.set_path(path)
        if not self._replaying:
            self._history.visit(path)
        self._sync_buttons()
        self._render()

    def _on_filter(self, text: str) -> None:
        self._filter = text.strip().lower()
        self._render()

    def focus_filter(self) -> None:
        self._filter_edit.setFocus()
        self._filter_edit.selectAll()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if (
            obj is self._filter_edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self._filter_edit.clear()
            self._table.setFocus()
            return True
        return super().eventFilter(obj, event)

    def _on_sort(self, column: int) -> None:
        if column == _SYNC:
            return  # the comparison marks have no order worth sorting by
        if column == self._sort_column:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_column, self._sort_desc = column, False
        order = (
            Qt.SortOrder.DescendingOrder
            if self._sort_desc
            else Qt.SortOrder.AscendingOrder
        )
        self._table.horizontalHeader().setSortIndicator(column, order)
        self._render()
        self.sort_changed.emit(self._sort_column, self._sort_desc)

    @property
    def sort_column(self) -> int:
        return self._sort_column

    def _sorted(self) -> list[RemoteEntry]:
        """The listing in display order: folders first, then the chosen column.

        Folders keep the top of the list whichever column is sorted - that is
        what a file manager does - so the date they are ordered by has to be a
        date worth ordering by. A directory's own mtime is not: it moves only
        when something is created or deleted directly inside it, so a file
        edited three levels down leaves every parent looking untouched. The
        listing therefore sorts on the *newest thing below* a folder, which is
        what ``treestat`` fills in a moment after the rows appear (see
        ``FileManagerTab._request_local_stats``).

        Until it does - and on a connection where it cannot be worked out at
        all - a folder has no date, and an unknown date sorts to the bottom of
        the folder block in both directions rather than pretending to be 1970.
        """
        column = self._sort_column
        descending = self._sort_desc

        def key(entry: RemoteEntry):
            if column == _SIZE:
                return (entry.size, entry.name.lower())
            if column == _MODIFIED:
                return (entry.modified or 0.0, entry.name.lower())
            if column == _MODE:
                return (
                    entry.mode if entry.mode is not None else -1,
                    entry.name.lower(),
                )
            return entry.name.lower()

        def unknown(entry: RemoteEntry) -> bool:
            if column == _MODIFIED:
                return entry.modified is None
            if column == _MODE:
                return entry.mode is None
            return False

        ordered = sorted(self._entries, key=key, reverse=descending)
        # Two stable passes: what has no value goes last, then folders go
        # first. Both keep the ordering the sort above worked out.
        ordered.sort(key=unknown)
        ordered.sort(key=lambda e: not e.is_dir)
        if self._filter:
            ordered = [e for e in ordered if self._filter in e.name.lower()]
        return ordered

    def _render(self) -> None:
        # Rebuilding the rows drops whatever was picked, and a re-render is
        # nearly always a refresh of the directory already on show: a sort
        # click, a filter, folder statistics landing - and, the one that
        # actually hurt, a background sync's queue draining. A commit-driven
        # sync submits one batch per sub-directory, so the pool falls idle
        # between them and both panes were being re-listed over and over
        # while the user was trying to use them. Losing the selection there
        # disarms every manual transfer at once: the buttons go dim, and a
        # drag begun a moment later finds nothing to carry, so it never
        # starts and nothing whatsoever appears to happen. Navigation clears
        # the selection itself before getting here, so what survives is only
        # ever a name in the directory it was picked in.
        chosen = {name for name, _ in self.selection()}
        table = self._table
        table.setRowCount(0)
        self._hover_row = -1
        if not self._at_root():
            self._append_row(_PARENT, "", "", "", is_parent=True)
        shown = self._sorted()
        for entry in shown:
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
        # Column widths are deliberately left alone here: re-fitting them on
        # every render (each sort click renders) collapsed whatever the user
        # had dragged the columns to.
        self._update_count(shown)
        if chosen:
            self._reselect(chosen)

    def _update_count(self, shown: list[RemoteEntry] | None = None) -> None:
        """The little truth next to the title: what is here, or what is picked."""
        selected = self.selected_entries()
        self.selection_changed.emit()
        if selected:
            size = sum(entry.size for entry in selected)
            self._count.setText(
                f"·  {len(selected)} selected — {_human_size(size)}"
            )
            return
        if shown is None:
            shown = self._sorted()
        folders = sum(1 for entry in shown if entry.is_dir)
        files = len(shown) - folders
        text = f"·  {folders} folder(s), {files} file(s)"
        if self._filter and len(shown) != len(self._entries):
            text += f" of {len(self._entries)}"
        self._count.setText(text if shown else "")

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
        first.setIcon(
            theme.entry_icon(
                _icon_kind(name or label, is_dir or is_parent), self._dark
            )
        )
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
        must not throw away the selection the user just made. _render keeps it
        for every caller now, so there is nothing extra to do here.
        """
        self._entries = list(entries)
        self._render()

    def _reselect(self, names: set[str]) -> None:
        """Pick these rows by name - all of them, in one go.

        selectRow() cannot be used in a loop here. Under ExtendedSelection it
        issues a ClearAndSelect, so each call threw away the last one and a
        multi-row selection came back as whichever row happened to match
        last: pick four files, let anything refresh the pane, and three of
        them were quietly gone. Handing the selection model one range per row
        keeps every one.
        """
        model = self._table.model()
        last_column = max(0, model.columnCount() - 1)
        wanted = QItemSelection()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, _NAME)
            if item is None:
                continue
            name, _is_dir, is_parent = item.data(Qt.ItemDataRole.UserRole)
            if not is_parent and name in names:
                wanted.select(model.index(row, 0), model.index(row, last_column))
        self._table.selectionModel().select(
            wanted,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

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

    def mark_side(self, *, remote: bool, live: bool = False) -> None:
        """Colour this pane's path bar by what it is: yours, or the server's."""
        self._path_bar.mark_side(remote=remote, live=live)

    def set_theme(self, dark: bool) -> None:
        """Repaint the navigation and listing glyphs for the current theme."""
        for kind, button in self._nav_buttons.items():
            button.setIcon(theme.nav_icon(kind, dark))
        self._hover_colour = _hover_colour(dark)
        if dark != self._dark:
            self._dark = dark
            self._render()  # the row icons carry the old palette otherwise

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
        self._path_bar.setEnabled(not busy)

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

    def _on_mouse_back(self) -> None:
        """The mouse's Back button: back through history, or up when empty."""
        if self._history.can_go_back():
            self.go_back()
        else:
            self._go_up()

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
        else:
            self.open_file.emit(name)

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
    #: This connection's saved settings changed and want writing to disk.
    profile_changed = pyqtSignal(object)

    # Requests handed to the worker thread.
    _open_requested = pyqtSignal(object)
    _list_requested = pyqtSignal(str)
    _home_requested = pyqtSignal()
    _mkdir_requested = pyqtSignal(str)
    _mkfile_requested = pyqtSignal(str)
    #: The whole selection at once: [(remote path, is_dir), ...]. One
    #: signal rather than one per file, because the worker answers each of
    #: them with a full directory listing and thirty of those is the wait.
    _delete_requested = pyqtSignal(object)
    _rename_requested = pyqtSignal(str, str)
    _download_requested = pyqtSignal(object, str)
    #: A whole tree, grouped by destination folder, as one queue.
    _download_groups_requested = pyqtSignal(object)
    _upload_requested = pyqtSignal(object, str)
    #: A whole tree, grouped by sub-directory, as one queue: (payload,
    #: base directory, quiet). See TransferWorker.upload_groups.
    _upload_groups_requested = pyqtSignal(object, str, bool)
    #: MCP-bridge work that is not a transfer, carrying a request id so the
    #: answer can be matched to the caller blocked on it.
    _bridge_delete_requested = pyqtSignal(str, object)
    _bridge_mkdir_requested = pyqtSignal(str, str)
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
    #: Ask the worker to name the directories inside one remote folder.
    _folders_requested = pyqtSignal(str)
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
    _retry_failed_requested = pyqtSignal()
    _retry_item_requested = pyqtSignal(str)
    _workers_requested = pyqtSignal(int)
    _options_changed = pyqtSignal(object)
    _edit_requested = pyqtSignal(str, str)
    _upload_quiet_requested = pyqtSignal(object, str, bool)

    #: Marshals watcher callbacks (a plain thread) onto the GUI thread.
    _watch_changes = pyqtSignal(object)
    #: Marshals a synced folder's local changes onto the GUI thread.
    _sync_changes = pyqtSignal(str, object)
    #: Marshals a synced folder's git commits onto the GUI thread.
    _sync_commit = pyqtSignal(str, object)
    #: Marshals "what did that commit touch" answers onto the GUI thread:
    #: (rule_id, repo root, list of (status, path) or None).
    _commit_diff_ready = pyqtSignal(str, str, object)
    #: Marshals commits seen in the (unsynced) repository the local pane is
    #: browsing onto the GUI thread.
    _pane_commit = pyqtSignal(object)
    #: The diff for such a commit: (repo root, CommitEvent, changes or None).
    _pane_commit_diff = pyqtSignal(str, object, object)
    #: Marshals background local folder statistics onto the GUI thread.
    _local_stats_ready = pyqtSignal(str, object)
    #: An old version of some files has been extracted from git and is
    #: ready to upload: (commit sha, rule id, repository root, Export).
    _commit_export_ready = pyqtSignal(str, str, str, object)

    def __init__(
        self,
        profile: ServerProfile,
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
        settings: Settings | None = None,
        jump: object = None,
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
        #: Watches the repository the local pane is inside even when no sync
        #: rule exists, so a commit there can offer itself for pushing.
        self._repo_watcher: GitCommitWatcher | None = None
        #: Folders this connection keeps on the server, and their watchers.
        self._sync_store = SyncRuleStore()
        self._sync_watchers: dict[str, DirectoryWatcher] = {}
        self._git_watchers: dict[str, GitCommitWatcher] = {}
        #: Rules whose trigger fired while the connection was down.
        self._sync_pending: set[str] = set()
        #: The last commit noticed in the pane's repository and what it
        #: touched, so it can still be pushed after the notice is gone:
        #: {"repo", "detail", "short", "changes"}.
        self._last_commit: dict | None = None
        #: The commit whose offer is still on the strip, kept so the offer can
        #: be worked out again for a new folder pair when either pane moves.
        self._pending_commit: dict | None = None
        #: Whether that offer is still waiting for an answer.
        self._commit_notice_open = False
        #: The folder pair the strip was last worked out for, so a refresh of
        #: a listing does not re-stat a whole commit for the same answer.
        self._commit_notice_pair: tuple[str, str] | None = None
        #: One-shot rules made by "Sync now" on a folder that is not armed.
        self._sync_transient: dict[str, SyncRule] = {}
        #: Scope the next new rule gets, until told otherwise.
        self._sync_recursive_default = True
        #: Rules with a scan in flight, and how often each has been re-queued.
        self._sync_running: set[str] = set()
        self._sync_retries: dict[str, int] = {}
        #: Per rule, the activity-log entry still waiting for its scan result.
        self._activity_events: dict[str, object] = {}
        #: Per rule, the subject of the commit whose diff is still being read,
        #: so the queue batch it turns into can say which commit it is.
        self._commit_notes: dict[str, str] = {}
        #: Scratch copies that were asked for by name: copy -> editor. The
        #: choice is made when the download is requested and has to survive
        #: until the file lands, which is a round trip later.
        self._edit_editors: dict[str, editors.Editor] = {}
        #: The editor found for the current preference, worked out once per tab
        #: rather than per right-click: looking for five programs means walking
        #: PATH, and a PATH with a network drive on it is not free. Installing
        #: an editor while the app is open shows up in the next tab.
        self._editor_found: tuple[str, editors.Editor | None] | None = None
        #: Remote files being edited locally: scratch copy -> remote path.
        self._edit_watch: dict[str, str] = {}
        #: Transfers handed to this tab by the MCP bridge and not yet
        #: finished, matched to their queue rows. See accept_bridge_upload.
        self._bridge_jobs: list[dict] = []
        #: Bridge deletes and folder creations waiting on the worker, by
        #: request id. See accept_bridge_delete.
        self._bridge_ops: dict[str, object] = {}
        self._edit_mtimes: dict[str, float] = {}
        self._edit_dirty: dict[str, float] = {}
        self._edit_root = os.path.join(
            tempfile.gettempdir(), "Sitekeeper", "edit", uuid.uuid4().hex[:8]
        )
        #: Where files pulled out of git's history are written before being
        #: uploaded. Never inside the working tree: publishing an old version
        #: must not put an old version back on disk.
        self._export_root = os.path.join(
            tempfile.gettempdir(), "Sitekeeper", "publish", uuid.uuid4().hex[:8]
        )
        #: Per commit being published, the activity-log entry waiting for it.
        self._publish_events: dict[str, object] = {}
        #: Per commit being published, its subject, read while it is extracted.
        self._publish_notes: dict[str, str] = {}
        self._edit_timer = QTimer(self)
        self._edit_timer.setInterval(1500)
        self._edit_timer.timeout.connect(self._poll_edits)
        self._diff_report = None
        self._diff_local = ""
        self._diff_remote = ""
        self._mirroring = False
        self._mirror_local_base = ""
        self._mirror_remote_base = ""
        # The same memory the shell tab uses: both are 'commands I ran
        # on this server', and keeping two of them meant the quick
        # runner and the shell each forgot what the other had done.
        self._shell_history = ShellHistory(profile.id)
        self._dialogs: dict[str, QWidget] = {}
        self._jump = jump
        self._spec = _spec_for(profile, jump)

        self._build_ui()
        self._refresh_actions()
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

        layout.addWidget(self._build_header())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 8)
        body_layout.setSpacing(8)
        layout.addWidget(body, 1)
        layout = body_layout  # everything below lives inside the padded body

        self._notice = _NoticeBar()
        layout.addWidget(self._notice)

        self._local = _FilePane("Local")
        self._local.bind_paths(self._local_child, self._local_parent)
        self._local.navigate.connect(self._load_local)
        self._local.open_file.connect(self._on_open_local)
        self._local.delete_requested.connect(self._on_delete)
        self._local.focused.connect(lambda: self._set_active(remote=False))
        self._local.menu_requested.connect(lambda pos: self._show_menu(pos, remote=False))
        self._local.paths_dropped.connect(self._on_local_drop)
        self._local.transfer_dropped.connect(self._on_pane_drop)
        self._local.browse.connect(self._on_browse_local)
        self._local.sort_changed.connect(
            lambda column, _desc: self._on_sort_changed(column, remote=False)
        )
        self._local.selection_changed.connect(self._refresh_actions)

        self._remote = _FilePane(f"Remote — {self._profile.label}", posix=True)
        self._remote.bind_paths(self._remote_child, self._remote_parent)
        self._remote.navigate.connect(self._list_remote)
        self._remote.open_file.connect(self._on_edit_remote)
        self._remote.delete_requested.connect(self._on_delete)
        self._remote.focused.connect(lambda: self._set_active(remote=True))
        self._remote.menu_requested.connect(lambda pos: self._show_menu(pos, remote=True))
        self._remote.paths_dropped.connect(self._on_remote_drop)
        self._remote.transfer_dropped.connect(self._on_pane_drop)
        self._remote.browse.connect(self._on_browse_remote)
        self._remote.sort_changed.connect(
            lambda column, _desc: self._on_sort_changed(column, remote=True)
        )
        self._remote.selection_changed.connect(self._refresh_actions)
        self._remote.mark_side(remote=True, live=self._is_production)
        self._local.mark_side(remote=False)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.addWidget(self._local)
        panes.addWidget(self._remote)
        panes.setSizes([500, 500])

        # What the watcher has seen, waiting to be sent. Above the queue on
        # purpose: this is the deciding, the queue below it is the doing.
        self._changes_panel = ChangesPanel()
        self._changes_panel.setVisible(False)
        self._changes_panel.upload_requested.connect(self._on_changes_upload)
        self._changes_panel.reveal_requested.connect(self._on_changes_reveal)
        self._changes_panel.count_changed.connect(self._on_changes_count)
        self._changes_panel.selection_changed.connect(
            lambda _count: self._refresh_actions()
        )

        self._queue_panel = TransferQueuePanel(workers=self._settings.transfer_workers)
        self._queue_panel.setVisible(False)
        self._queue_panel.pause_requested.connect(self._pause_requested.emit)
        self._queue_panel.resume_requested.connect(self._resume_requested.emit)
        self._queue_panel.cancel_all_requested.connect(self._on_cancel)
        self._queue_panel.cancel_item_requested.connect(self._cancel_item_requested.emit)
        self._queue_panel.prioritize_item_requested.connect(self._prioritize_requested.emit)
        self._queue_panel.reorder_requested.connect(self._reorder_requested.emit)
        self._queue_panel.clear_finished_requested.connect(self._on_clear_finished)
        self._queue_panel.retry_failed_requested.connect(self._retry_failed_requested.emit)
        self._queue_panel.retry_item_requested.connect(self._retry_item_requested.emit)
        self._queue_panel.workers_changed.connect(self._workers_requested.emit)

        # The queue shares a vertical splitter with the panes, so its height
        # is the user's to drag - down to a sliver when it is in the way.
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(panes)
        split.addWidget(self._changes_panel)
        split.addWidget(self._queue_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 1)
        split.setCollapsible(0, False)
        layout.addWidget(split, 1)

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

        self._state_pill = QLabel("Connecting…")
        self._state_pill.setObjectName("pill")
        self._state_pill.setProperty("state", "busy")
        row.addWidget(self._state_pill)
        if self._is_production:
            row.addWidget(
                theme.production_badge("Files here are live. Uploads take effect at once.")
            )
        row.addSpacing(10)

        self._mirror_box = QCheckBox("Mirror")
        self._mirror_box.setToolTip(
            "Keep both panes in step: entering a folder on one side enters the "
            "matching folder on the other"
        )
        self._mirror_box.setChecked(self._settings.mirror_navigation)
        self._mirror_box.toggled.connect(self._on_mirror_toggled)
        row.addWidget(self._mirror_box)

        self._watch_box = QCheckBox("Watch")
        self._watch_box.setToolTip(self._watch_tooltip())
        self._watch_box.toggled.connect(self._on_watch_toggled)
        row.addWidget(self._watch_box)

        row.addSpacing(12)

        # Comparing is occasional - a question you ask now and then, not a
        # mode you sit in - so it lives in the Sync menu, on F9, and in both
        # context menus rather than taking permanent space in the bar.
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

        # Ticking Watch opens the list; this is how it is reopened after it
        # has been closed, and where the count lives so a tab that noticed
        # something says so without being looked at.
        self._changes_btn = QPushButton("Changes")
        self._changes_btn.setCheckable(True)
        self._changes_btn.setToolTip("Show what Watch has seen change on this machine")
        self._changes_btn.toggled.connect(self._changes_panel_visible)
        row.addWidget(self._changes_btn)

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
                (
                    f"Open this folder in {self._editor_name()} over SSH",
                    "",
                    self._open_remote_folder_in_editor,
                ),
            ),
        )
        # Hidden until the connection says it really has a shell.
        self._server_btn.setVisible(False)
        self._shell_buttons: list[QWidget] = [self._server_btn]
        row.addWidget(self._server_btn)
        return bar

    def _watch_tooltip(self) -> str:
        if self._settings.watch_autosync:
            return (
                "Notice local edits as they are saved, and upload them at "
                "once (auto-upload is on in Settings)"
            )
        return (
            "List local edits as they are saved, so you can pick what to "
            "upload - files or whole folders"
        )

    def _build_footer(self) -> QWidget:
        """The transfer bar: what acts on the current selection."""
        bar = QWidget()
        bar.setObjectName("footerbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(6)

        # Upload and Download are the same pair of buttons in the same place
        # always - what changes is which of them is *the* action right now,
        # and that follows the pane you are working in. Two loud buttons would
        # be no louder than none; see _refresh_actions.
        self._upload_btn = QPushButton("▲ Upload")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        self._download_btn = QPushButton("▼ Download")
        self._download_btn.setObjectName("secondary")
        self._download_btn.clicked.connect(self._on_download)
        row.addWidget(self._upload_btn)
        row.addWidget(self._download_btn)

        row.addSpacing(12)

        mkdir_btn = QPushButton("New folder")
        mkdir_btn.setToolTip(
            "F7. A path makes every folder in it: releases/2026/08"
        )
        mkdir_btn.clicked.connect(self._on_mkdir)
        mkfile_btn = QPushButton("New file")
        mkfile_btn.setToolTip(
            "Shift+F4. Creates it empty; an existing file is left alone"
        )
        mkfile_btn.clicked.connect(self._on_mkfile)
        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(self._on_rename)
        delete_btn = QPushButton("Delete")
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self._on_delete)
        for button in (mkdir_btn, mkfile_btn, rename_btn, delete_btn):
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
            ("Ctrl+F", lambda: self._active_pane().focus_filter()),
            ("F2", self._on_rename),
            ("F7", self._on_mkdir),
            ("Shift+F4", self._on_mkfile),
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
        self._changes_panel.set_theme(enable)
        activity = self._activity(create=False)
        if activity is not None:
            activity.set_theme(enable)
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
            preserve_times=settings.preserve_times,
            # Stored in kilobytes because that is the unit anybody setting a
            # limit thinks in; the pool counts bytes.
            rate_limit=max(0, settings.transfer_rate_kb) * 1024,
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
            (self._mkfile_requested, self._worker.make_file),
            (self._delete_requested, self._worker.delete_entries),
            (self._rename_requested, self._worker.rename_entry),
            (self._download_requested, self._worker.run_download),
            (self._download_groups_requested, self._worker.download_groups),
            (self._upload_requested, self._worker.run_upload),
            (self._upload_groups_requested, self._worker.upload_groups),
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
            (self._folders_requested, self._worker.request_folders),
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
            (self._retry_failed_requested, self._worker.retry_failed),
            (self._retry_item_requested, self._worker.retry_item),
            (self._workers_requested, self._worker.set_workers),
            (self._options_changed, self._worker.update_options),
            (self._edit_requested, self._worker.fetch_for_edit),
            (self._upload_quiet_requested, self._worker.upload_quietly),
            (self._bridge_delete_requested, self._worker.bridge_delete),
            (self._bridge_mkdir_requested, self._worker.bridge_make_dir),
        )
        for signal, slot in outgoing:
            signal.connect(slot)

        incoming = (
            (self._worker.connected, self._on_connected),
            (self._worker.capabilities_ready, self._on_capabilities),
            (self._worker.failed, self._on_failed),
            (self._worker.host_key_unknown, self._on_host_key_unknown),
            (self._worker.listing, self._on_listing),
            (self._worker.op_failed, self._on_op_message),
            (self._worker.op_done, self._on_op_message),
            (self._worker.queue_started, self._on_queue_started),
            (self._worker.progress, self._on_progress),
            (self._worker.file_finished, self._on_file_finished),
            (self._worker.queue_finished, self._on_queue_finished),
            (self._worker.queue_item, self._queue_panel.update_item),
            (self._worker.queue_item, self._on_activity_item),
            (self._worker.queue_item, self._on_bridge_item),
            (self._worker.bridge_op, self._on_bridge_op),
            (self._worker.queue_stats, self._queue_panel.update_stats),
            (self._worker.tool_result, self._on_tool_result),
            (self._worker.tool_failed, self._on_tool_failed),
            (self._worker.tool_progress, self._on_tool_progress),
            (self._worker.folders_listed, self._on_folders_listed),
            (self._worker.folders_failed, self._on_folders_failed),
            (self._worker.edit_ready, self._on_edit_ready),
            (self._worker.closed, self._on_closed),
        )
        for signal, slot in incoming:
            signal.connect(slot)

        self._watch_changes.connect(self._on_watch_changes)
        self._sync_changes.connect(self._on_sync_changes)
        self._sync_commit.connect(self._on_sync_commit)
        self._commit_diff_ready.connect(self._on_commit_diff)
        self._pane_commit.connect(self._on_pane_commit)
        self._pane_commit_diff.connect(self._on_pane_commit_diff)
        self._local_stats_ready.connect(self._on_local_stats)
        self._commit_export_ready.connect(self._on_commit_export)
        self._thread.start()

    def _connect_to_server(self) -> None:
        profile = self._profile
        self._set_connection_state("busy", f"Connecting to {profile.describe_target()}")
        self._set_status(f"Connecting to {profile.describe_target()} …")
        self._open_requested.emit(self._spec)

    def _on_host_key_unknown(self, unknown: object) -> None:
        """First contact with this server: show its fingerprint and ask.

        Answering yes records the key and connects; answering no leaves the tab
        disconnected and says why, rather than retrying into the same question.
        """
        from mysql_runner.ui.host_key_dialog import ask

        if not isinstance(unknown, hostkeys.HostKeyUnknown):
            return
        if ask(unknown, self):
            self._set_status("Server confirmed. Connecting…")
            self._connect_to_server()
            return
        self._on_failed(
            f"Not connected: {unknown.host} was not confirmed as your server."
        )

    # ----- worker callbacks -----------------------------------------------
    def _on_connected(self, banner: str) -> None:
        self._connected = True
        self._set_connection_state("ok", banner)
        self._refresh_actions()
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
        self._set_connection_state("fail", message)
        self._refresh_actions()
        self._set_status(message)
        self.status_message.emit(f"{self._profile.label}: {message}")
        QMessageBox.warning(self, "Connection failed", message)

    def _on_listing(self, path: str, entries: object) -> None:
        assert isinstance(entries, list)
        self._remote.set_listing(path, entries, at_root=path in ("/", ""))
        self._apply_diff_marks()
        self._refresh_actions()  # the folder the buttons name just changed
        self._request_remote_stats(path, entries)
        if self._mirror_box.isChecked():
            self._mirror_to_local(path)
        # The other half of the pair a commit offer names - see _load_local.
        self._show_pane_commit_notice()

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

    def _on_queue_started(self, total: int, origin: str = "") -> None:
        self._queue_total = total
        self._queue_done = 0
        self._queue_panel.start_batch(total, origin)
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
        self._abandon_bridge_jobs("the connection closed before it finished")
        if self._closing:
            return
        self._set_connection_state("fail", "The connection was closed.")
        self._refresh_actions()
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
    # A folder's own timestamp is not the date anybody means by "when did this
    # change?" - it moves when an entry is added or removed directly inside,
    # and not when a file three levels down is edited. So the listing shows,
    # and sorts by, the newest thing *below* a folder, which costs a walk of
    # the tree. That walk is paid for lazily: automatically while the setting
    # is on, and on demand the moment somebody sorts by Modified, which is the
    # one click that makes the difference visible.
    def _on_sort_changed(self, column: int, *, remote: bool) -> None:
        """Sorting by Modified is a request for real folder dates."""
        if column != _MODIFIED:
            return
        pane = self._remote if remote else self._local
        if not pane.path:
            return
        if remote:
            self._request_remote_stats(pane.path, pane.entries, forced=True)
        else:
            self._request_local_stats(pane.path, pane.entries, forced=True)

    def _request_remote_stats(
        self, path: str, entries: list, *, forced: bool = False
    ) -> None:
        if not (self._settings.folder_stats or forced):
            return
        names = [entry.name for entry in entries if entry.is_dir and not entry.is_link]
        if not names or len(names) > _MAX_STAT_FOLDERS:
            if names and forced:
                self._set_status(
                    f"{len(names)} folders here is too many to measure; their "
                    "dates are the server's own."
                )
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

    def _request_local_stats(
        self, path: str, entries: list, *, forced: bool = False
    ) -> None:
        if not (self._settings.folder_stats or forced):
            return
        names = [entry.name for entry in entries if entry.is_dir and not entry.is_link]
        if not names or len(names) > _MAX_STAT_FOLDERS:
            if names and forced:
                self._set_status(
                    f"{len(names)} folders here is too many to measure; their "
                    "dates are the folders' own."
                )
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
            self._set_status(_missing_local_reason(target))
            return
        try:
            entries = _scan_local(target)
        except OSError as exc:
            self._set_status(f"Cannot read {target}: {exc}")
            return
        at_root = os.path.dirname(target) == target
        self._local.set_listing(target, entries, at_root=at_root)
        self._apply_diff_marks()
        self._refresh_actions()
        self._request_local_stats(target, entries)
        if self._mirror_box.isChecked():
            self._mirror_to_remote(target)
        self._apply_sync_marks()
        if self._watcher is not None and self._watcher.root != target:
            self._restart_watcher(target)
        self._watch_pane_repo(target)
        # A live commit offer is between two folders, and one of them just
        # changed, so what the strip says has to be worked out again.
        self._show_pane_commit_notice()

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
    def _on_mirror_toggled(self, enabled: bool) -> None:
        """(Re)anchor mirroring at the pair of directories on screen now.

        The anchor used to survive from whenever mirroring was first used, so
        turning it on after browsing elsewhere left it matched against
        directories long gone - it looked simply broken. Enabling always
        anchors afresh; disabling forgets, so the next enable does too.
        """
        self._mirror_local_base = self._local.path if enabled else ""
        self._mirror_remote_base = self._remote.path if enabled else ""

    def _mirror_to_local(self, remote_path: str) -> None:
        if self._mirroring or not self._mirror_bases_known():
            return
        target = mirror_path(self._mirror_remote_base, remote_path,
                             self._mirror_local_base, posix=False)
        if not target:
            # The user left the anchored tree. Follow them - the pair on
            # screen becomes the new anchor - rather than going silent.
            self._mirror_remote_base = remote_path
            self._mirror_local_base = self._local.path
            return
        if target == self._local.path:
            return
        if not os.path.isdir(target):
            self._set_status(f"Mirror: there is no {target} on this side.")
            return
        self._mirroring = True
        try:
            self._load_local(target)
        finally:
            self._mirroring = False

    def _mirror_to_remote(self, local_path: str) -> None:
        if self._mirroring or not self._mirror_bases_known():
            return
        target = mirror_path(self._mirror_local_base, local_path,
                             self._mirror_remote_base, posix=True)
        if not target:
            self._mirror_local_base = local_path
            self._mirror_remote_base = self._remote.path
            return
        if target == self._remote.path:
            return
        self._mirroring = True
        try:
            self._list_remote(target)
        finally:
            self._mirroring = False

    def _mirror_bases_known(self) -> bool:
        """Whether mirroring has a pair to work from, anchoring it if it can."""
        if not self._mirror_local_base or not self._mirror_remote_base:
            if self._local.path and self._remote.path:
                self._mirror_local_base = self._local.path
                self._mirror_remote_base = self._remote.path
            else:
                return False
        return True

    # ----- opening and editing files ---------------------------------------
    # Double-clicking a local file opens it the way Explorer would. Double-
    # clicking a remote file downloads it to a scratch folder, opens it the
    # same way, and from then on every save is noticed and uploaded back -
    # because "download, edit, re-upload, delete the copy" is exactly the loop
    # this app exists to remove.
    def _on_open_local(self, name: str) -> None:
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(self._local_child(name)))

    def _on_edit_remote(
        self, name: str, *, editor: editors.Editor | None = None
    ) -> None:
        if not self._require_connection():
            return
        entry = self._remote.entry(name)
        if entry is not None and entry.size > _MAX_EDIT_BYTES:
            confirm = QMessageBox.question(
                self,
                "Large file",
                f"{name} is {_human_size(entry.size)}. Download it for "
                "editing anyway?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        # One question up front covers the saves that follow: agreeing to
        # edit a production file is agreeing to upload the edits.
        if not self._confirm_production(f"edit {name} (each save uploads to)"):
            return
        remote = self._remote_child(name)
        local = os.path.join(self._edit_root, uuid.uuid4().hex[:8], name)
        if editor is not None:
            self._edit_editors[local] = editor
        where = f" for {editor.name}" if editor is not None else " for editing"
        self._set_status(f"Fetching {name}{where}…")
        self._edit_requested.emit(remote, local)

    def _on_edit_ready(self, local: str, remote: str) -> None:
        try:
            self._edit_mtimes[local] = os.path.getmtime(local)
        except OSError as exc:
            self._set_status(f"{os.path.basename(local)}: {exc}")
            return
        self._edit_watch[local] = remote
        if not self._edit_timer.isActive():
            self._edit_timer.start()
        name = os.path.basename(local)
        editor = self._edit_editors.pop(local, None)
        opened_with = ""
        if editor is not None:
            try:
                editors.open_paths(editor, [local])
                opened_with = f" in {editor.name}"
            except OSError as exc:
                # The file is here and the watch is armed; falling back to
                # whatever Windows opens it with is better than a status line
                # about a program the user cannot do anything about right now.
                self._set_status(f"{editor.name} would not start ({exc}).")
        if not opened_with:
            from PyQt6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(local))
        self._set_status(
            f"Editing {name}{opened_with} - every save uploads back to "
            f"{remote} while this tab is open."
        )

    def _poll_edits(self) -> None:
        """Notice saves to edited copies, once their timestamps hold still.

        A save is only pushed when the same new timestamp is seen twice, so an
        editor still writing a large file is never caught mid-write.
        """
        for local, remote in list(self._edit_watch.items()):
            try:
                stamp = os.path.getmtime(local)
            except OSError:
                continue  # mid-save, or the copy is gone; look again next tick
            if stamp <= self._edit_mtimes.get(local, 0.0):
                continue
            if self._edit_dirty.get(local) != stamp:
                self._edit_dirty[local] = stamp
                continue
            self._edit_dirty.pop(local, None)
            self._edit_mtimes[local] = stamp
            self._push_edit(local, remote)

    def _push_edit(self, local: str, remote: str) -> None:
        name = os.path.basename(local)
        if not self._connected:
            self._set_status(f"{name} changed, but the connection is down.")
            return
        self._upload_quiet_requested.emit(
            ([(local, False)], IgnoreRules.empty(), "edit"),
            RemoteFS.parent(remote),
            False,
        )
        self._set_status(f"Uploading {name} → {remote}")

    # ----- VS Code --------------------------------------------------------
    # Two jobs, one editor, and the difference is worth knowing before you pick
    # an entry. A *file* is fetched to a scratch copy and every save goes back
    # up - the loop above, aimed at a named program instead of whatever Windows
    # has registered for .php. A *folder* is not fetched at all: VS Code opens
    # it over its own SSH session and edits the files where they are, which is
    # the only way its search, its git and its terminal are the server's.
    # Remote-SSH authenticates itself, so it never sees this app's stored
    # password - see transfer/editors.py.
    def _editor(self) -> editors.Editor | None:
        """The editor to open things in, or None when none is installed."""
        wanted = self._settings.editor_program
        if self._editor_found is None or self._editor_found[0] != wanted:
            self._editor_found = (wanted, editors.find_editor(wanted))
        return self._editor_found[1]

    def _editor_name(self) -> str:
        """What to call it in a menu, whether or not it is installed."""
        editor = self._editor()
        return editor.name if editor is not None else "VS Code"

    def _open_remote_in_editor(self, name: str) -> None:
        """Fetch one server file into the editor, saves going back up."""
        editor = self._editor()
        if editor is None:
            self._no_editor_note()
            return
        self._on_edit_remote(name, editor=editor)

    def _open_remote_folder_in_editor(self) -> None:
        """Open a server folder in place, over the editor's own SSH session."""
        editor = self._editor()
        if editor is None:
            self._no_editor_note()
            return
        blocked = self._remote_ssh_block()
        if blocked:
            QMessageBox.information(self, "Not over this connection", blocked)
            return
        selection = self._remote.selection()
        if len(selection) == 1 and selection[0][1]:
            path = self._remote_child(selection[0][0])
        else:
            path = self._remote.path or "/"
        # Nothing is queued and nothing is compared: from here on the editor
        # writes to the server directly, which on a production box is exactly
        # the thing the guard exists for.
        if not self._confirm_production(
            f"open {path} in {editor.name}, which then writes straight to"
        ):
            return
        target = editors.RemoteTarget(
            host=self._profile.host,
            port=self._profile.effective_port,
            username=self._profile.username,
        )
        try:
            editors.open_remote(editor, target, path)
        except OSError as exc:
            QMessageBox.warning(self, f"Could not start {editor.name}", str(exc))
            return
        self._set_status(
            f"{editor.name} is opening {path} on {target.authority()}. It "
            "connects itself, so it asks for the key or password rather than "
            "taking this connection's."
        )

    def _open_local_in_editor(self) -> None:
        """Open the local selection - or the folder on show - in the editor."""
        editor = self._editor()
        if editor is None:
            self._no_editor_note()
            return
        selection = self._local.selection()
        paths = [self._local_child(name) for name, _is_dir in selection]
        if not paths and self._local.path:
            paths = [self._local.path]
        if not paths:
            self._set_status("There is nothing here to open.")
            return
        try:
            editors.open_paths(editor, paths)
        except OSError as exc:
            QMessageBox.warning(self, f"Could not start {editor.name}", str(exc))
            return
        what = paths[0] if len(paths) == 1 else f"{len(paths)} items"
        self._set_status(f"Opened {what} in {editor.name}.")

    def _remote_ssh_block(self) -> str:
        """Why the editor cannot open this connection's folders in place, or "".

        Said in full rather than by hiding the entry: "why is this greyed out"
        is a question with a real answer in every one of these cases, and two
        of them are answered by doing something the app cannot do for you.
        """
        if self._profile.kind != ConnectionKind.SFTP:
            return (
                "VS Code can only open a folder in place over SSH, and this is "
                f"an {self._profile.kind.value.upper()} connection. Opening a "
                "single file still works: it comes down as a copy and every "
                "save goes back up."
            )
        if self._profile.jump_profile_id or self._profile.proxy_command:
            return (
                "This connection is routed through a jump host or a proxy "
                "command, and there is no way to hand that to VS Code on a "
                "command line. Give the server a Host entry in ~/.ssh/config "
                "and open it from VS Code itself."
            )
        if not self._profile.host:
            return "This connection has no host name to give VS Code."
        return ""

    def _no_editor_note(self) -> None:
        QMessageBox.information(
            self,
            "No editor found",
            "Visual Studio Code, VS Code Insiders, Cursor, VSCodium and "
            "Windsurf were all looked for on this machine, and none of them "
            "is installed - or the one that is has no command line on PATH.\n\n"
            "Installing VS Code with its “Add to PATH” option ticked is "
            "enough. Settings ▸ File transfer picks between them when there "
            "is more than one.",
        )

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
        self._upload_tree(
            existing,
            self._diff_remote or self._remote.path,
            flatten=base,
            origin="compare",
        )

    def _download_relative(self, relatives: object) -> None:
        if not isinstance(relatives, list) or not self._require_connection():
            return
        base = self._diff_remote or self._remote.path
        items = [(RemoteFS.join(base, rel), False) for rel in relatives]
        self._download_tree(items, self._diff_local or self._local.path, flatten=base)

    def _upload_tree(
        self, items, remote_dir: str, *, flatten: str = "", rules=None,
        quiet: bool = False, origin: str = "",
    ) -> None:
        """Send items, keeping their layout relative to ``flatten`` if given.

        ``quiet`` sends them without the remote pane following the transfer.
        Anything a trigger starts has to be quiet: nobody asked to be taken
        anywhere. The refresh when the queue drains still repaints whatever
        directory the user is actually in.

        ``origin`` names the trigger, so the queue can group the batch under
        it and say what started an upload nobody pressed a button for.
        """
        ignore = rules if rules is not None else self._ignore_rules()
        if not flatten:
            if quiet:
                # True: a trigger's target can be a folder the commit only
                # just added, which the upload has to make for itself.
                self._upload_quiet_requested.emit(
                    (items, ignore, origin), remote_dir, True
                )
            else:
                self._upload_requested.emit((items, ignore, origin), remote_dir)
            return
        # Group by their sub-directory so nested files land in the right
        # place - but hand the lot over in one go. Sending a group at a time
        # made each its own queue, and a sync of a twenty-folder site then
        # started, drained and reported twenty separate batches, re-listing
        # the server and reloading the local pane after every one.
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for path, is_dir in items:
            rel_dir = os.path.dirname(os.path.relpath(path, flatten))
            target = RemoteFS.join(remote_dir, rel_dir.replace("\\", "/")) if rel_dir else remote_dir
            by_dir.setdefault(target, []).append((path, is_dir))
        if not by_dir:
            return
        self._upload_groups_requested.emit(
            (list(by_dir.items()), ignore, origin), remote_dir, quiet
        )

    def _download_tree(self, items, local_dir: str, *, flatten: str = "") -> None:
        if not flatten:
            self._download_requested.emit((items, self._ignore_rules()), local_dir)
            return
        # Grouped by destination folder, then sent as one queue - see
        # _upload_tree for why a group at a time was the wrong shape. The
        # folders themselves are made on the worker thread with the rest.
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for path, is_dir in items:
            rel = path[len(flatten.rstrip("/")) + 1:] if path.startswith(flatten) else ""
            rel_dir = os.path.dirname(rel)
            target = os.path.join(local_dir, rel_dir.replace("/", os.sep)) if rel_dir else local_dir
            by_dir.setdefault(target, []).append((path, is_dir))
        if not by_dir:
            return
        self._download_groups_requested.emit(
            (list(by_dir.items()), self._ignore_rules(), "compare")
        )

    # ----- the watcher ---------------------------------------------------
    def _on_watch_toggled(self, watching: bool) -> None:
        if watching:
            self._restart_watcher(self._local.path)
            # The list is the point of watching, so it opens with the watch
            # rather than waiting for the first save to justify itself.
            self._changes_btn.setChecked(True)
            return
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            self._set_status("Stopped watching.")
        # Whatever it already found stays on screen: the files are still
        # different from the server, and closing the list would be the old
        # behaviour of losing the answer as soon as it was given.
        if not self._changes_panel.count():
            self._changes_btn.setChecked(False)

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
        self._changes_panel.set_root(path)
        auto = (
            " and uploading changes"
            if self._settings.watch_autosync
            else "; saves will be listed below"
        )
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
            # The answer to "what changed?" is a list you can act on, not a
            # status line the next message wipes.
            self._changes_panel.add_changes(changes)
            self._changes_btn.setChecked(True)
            self._set_status(f"Changed locally: {text}")
            return
        if not self._connected:
            self._set_status(f"Changed locally, but not connected: {text}")
            return
        self._upload_changes(changes)

    def _upload_changes(self, changes: list[Change]) -> list[Change]:
        """Send these changed files to the matching remote folders.

        Returns what was actually handed to the queue, which is not always
        what was asked for: a file deleted between being noticed and being
        sent is dropped here, and a refused production confirmation drops the
        lot. The changed-files panel clears its rows from this, so a row only
        leaves the list when something really took it.
        """
        base = self._local.path
        wanted = [
            change
            for change in changes
            if change.kind in (ChangeKind.ADDED, ChangeKind.MODIFIED)
            and os.path.isfile(change.path)
        ]
        if not wanted:
            return []
        if self._is_production and self._settings.production_guard:
            # Auto-upload to production is exactly the accident this guard is
            # for, so it asks - once per batch, not once per file.
            if not self._confirm_production(f"upload {len(wanted)} changed file(s) to"):
                self._watch_box.setChecked(False)
                return []
        # Read the destination once, before anything goes up, and send the
        # batches quietly: the pane this target came from must not be moved by
        # the upload it is aiming, or the next save nests inside the last one.
        remote_base = self._remote.path
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for change in wanted:
            rel_dir = os.path.dirname(change.rel)
            target = (
                RemoteFS.join(remote_base, rel_dir) if rel_dir else remote_base
            )
            by_dir.setdefault(target, []).append((change.path, False))
        for target, group in by_dir.items():
            self._upload_quiet_requested.emit(
                (group, self._ignore_rules(), "watch"), target, True
            )
        self._set_status(f"Uploading {len(wanted)} changed file(s) from {base}.")
        return wanted

    # ----- the changed-files panel ----------------------------------------
    def _changes_panel_visible(self, visible: bool) -> None:
        self._changes_panel.setVisible(visible)
        # Opening or closing the list moves where the loud button belongs.
        self._refresh_actions()

    def _on_changes_count(self, count: int) -> None:
        """Carry the count onto the button, so a hidden list still speaks."""
        self._changes_btn.setText(f"Changes ({count})" if count else "Changes")

    def _on_changes_upload(self, changes: object) -> None:
        """Send the files ticked in the panel to the remote pane's folder."""
        if not isinstance(changes, list) or not changes:
            return
        if not self._connected:
            self._set_status("Not connected, so nothing was sent.")
            return
        sent = self._upload_changes(changes)
        if sent:
            self._changes_panel.take_uploaded(sent)
            self._queue_btn.setChecked(True)
        elif not any(os.path.isfile(change.path) for change in changes):
            # Gone from the disk between being noticed and being sent. A
            # declined production confirmation also returns nothing, but that
            # one has already said its piece in a dialog.
            self._set_status("Those files are no longer on this machine.")

    def _on_changes_reveal(self, path: object) -> None:
        """Show one changed file's folder in the local pane."""
        folder = os.path.dirname(str(path))
        if folder and os.path.isdir(folder):
            self._load_local(folder)

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

        The old answer was "only when the server refuses to be given a
        timestamp", on the reasoning that uploads carry the local mtime over,
        so a preserved timestamp is as good as a digest and enormously
        cheaper. That reasoning has one hole, and it is a big one: it assumes
        the local mtime means "when this content was written". After a clone,
        a pull, a checkout or a fresh CI workspace it does not - git stamps
        every file it writes with *now*, so a colleague who pulls the same
        commit gets timestamps hours newer than the identical bytes on the
        server. Everything then reads as changed, every sync re-uploads the
        whole tree, and nothing about it looks like a bug from the inside.

        So content is the default and the timestamp shortcut is opt-in.

        The cost is smaller than it sounds even at its worst. With a shell the
        whole remote tree is digested by one command (see
        ``remote_exec.digest_tree``) - a round trip, not a download. Without
        one, hashing does mean reading every remote file - but the alternative
        is worse, not better: every file that only *looks* changed is uploaded,
        and shadow backups download the copy being replaced first, so the
        timestamp shortcut spends a download *and* an upload per file to avoid
        spending one download. It is still switchable for the case where
        neither is wanted and the timestamps really can be trusted.
        """
        if self._settings.sync_compare_hashes:
            return True
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
        elsewhere = ""      # the pane's folder, when it is not the rule's own
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
            # Arming does not re-point a rule that already exists: a pane that
            # happens to be elsewhere must not silently move a working target.
            # Which one is kept is said outright below, because the difference
            # is otherwise discovered from the uploaded paths.
            implied = normalise_remote(self._remote_target_for(local))
            if implied and implied != rule.remote:
                elsewhere = implied
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
        aside = (
            f" The pane is on {elsewhere}, but this rule keeps {rule.remote} - "
            "Sync ▸ Synced folders… ▸ Server folder… changes it."
            if elsewhere
            else ""
        )
        self._set_status(
            f"Syncing {rule.local} to {rule.remote} {trigger}{because}.{aside}"
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

    def _on_sync_edit_remote(self, rule_id: str) -> None:
        """Re-point one rule at another folder on the server.

        Opens the folder tree on the rule's current target, so the usual case -
        "this is one level too deep" - is a click on the parent.
        """
        rule = self._sync_store.get(rule_id)
        if rule is None or not self._require_connection():
            return
        self._open_folder_picker(
            rule.remote or "/",
            f"Where {rule.name} belongs on {self._profile.label}.",
            lambda path: self._apply_sync_remote(rule_id, path),
        )

    def _apply_sync_remote(self, rule_id: str, remote: str) -> None:
        before = self._sync_store.get(rule_id)
        was = before.remote if before is not None else ""
        rule = self._sync_store.set_remote(rule_id, remote)
        if rule is None:
            self._set_status("That folder cannot be used as a sync target.")
            return
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        if rule.remote == was:
            self._set_status(f"{rule.name}: already syncing to {rule.remote}.")
            return
        self._set_status(
            f"{rule.name}: now syncing to {rule.remote} (was {was}). Nothing has "
            "been sent yet - use Sync now to bring the new folder into step."
        )
        self.status_message.emit(
            f"{self._profile.label}: {rule.name} now syncs to {rule.remote}"
        )

    def _on_sync_edit_local(self, rule_id: str) -> None:
        """Re-point one rule at another folder on this machine."""
        rule = self._sync_store.get(rule_id)
        if rule is None:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, f"The local side of {rule.remote}", rule.local
        )
        if not chosen:
            return
        clash = self._sync_store.find(self._profile.id, chosen)
        if clash is not None and clash.id != rule.id:
            self._set_status(
                f"{clash.name} already syncs that folder, to {clash.remote}. "
                "Two rules on one folder would be ambiguous."
            )
            return
        was = rule.local
        updated = self._sync_store.set_local(rule_id, chosen)
        if updated is None:
            self._set_status("That folder cannot be used as a sync source.")
            return
        # The watcher was rooted at the old folder, so it has to be replaced.
        self._start_sync_rule(updated)
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        self._set_status(
            f"Syncing {updated.local} to {updated.remote} (was {was})."
        )

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
            entry = self._activity_events.get(rule.id)
            if entry is not None:
                self._activity().set_outcome(
                    entry, "waiting for the connection…", kind="info"
                )
            self._set_status(f"{rule.name}: will sync once the connection is up.")
            return
        if rule.id in self._sync_running:
            return
        self._sync_running.add(rule.id)
        hashes = self._sync_with_hashes()
        self._tool_progress.start(f"Syncing {rule.name}…")
        # Say which comparison is about to run, because the two feel completely
        # different: on a server with no shell, hashing reads every remote file
        # and a first sync of a big tree is not quick. Better said up front
        # than diagnosed as a hang.
        how = "by content"
        if hashes and Capability.EXEC not in self._capabilities:
            how = "by content (reading every file - this server cannot hash)"
        elif not hashes:
            how = "by size and time"
        self._set_status(f"{rule.name}: comparing with {rule.remote}, {how}…")
        self._sync_scan_requested.emit(
            rule.local,
            rule.remote,
            hashes,
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
        log = self._activity()
        entry = log.log_event(
            f"Save — {rule.name}", detail=summarise(changes)
        )
        if uploads:
            log.add_files(
                entry,
                [
                    (change.path, local_relative(rule.local, change.path))
                    for change in uploads
                ],
            )
            self._upload_tree(
                [(change.path, False) for change in uploads],
                rule.remote,
                flatten=rule.local,
                rules=self._rule_ignores(rule),
                quiet=True,
                origin="sync",
            )
        gone = [rule.remote_for(change.path) for change in removals]
        gone = [path for path in gone if path and path != rule.remote]
        if gone:
            log.add_notes(entry, gone, outcome="removal requested")
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
        """A synced folder in on-commit mode saw its repository move.

        The commit itself says which files it changed, so those are what goes
        up - re-scanning a whole site against the server for every commit took
        minutes on big trees, and its removal pass kept tripping over files
        that only ever existed on the server (logs, caches, uploads). The full
        comparison remains the fallback for when git cannot answer.
        """
        rule = self._rule(rule_id)
        if rule is None:
            return
        detail = event.describe() if isinstance(event, CommitEvent) else ""
        headline = f"{rule.name}: commit {detail}" if detail else f"{rule.name}: commit"
        log = self._activity()
        stale = self._activity_events.get(rule_id)
        if stale is not None:
            log.set_outcome(stale, "superseded by a newer commit", kind="info")
        self._activity_events[rule_id] = log.log_event(
            f"Commit — {rule.name}",
            detail=(detail or "HEAD moved") + " — reading the commit…",
        )
        self._set_status(f"{headline} - syncing…")
        self.status_message.emit(f"{self._profile.label}: {headline}")
        if not isinstance(event, CommitEvent):
            self._sync_now(rule)
            return
        repo = find_repo(rule.local) or rule.local

        def measure() -> None:
            # git runs off the GUI thread; a repo on a network drive can
            # take a moment to answer. The subject is read here too: it costs
            # one more git call on a thread that is already making one, and
            # asking for it on the GUI thread to label a batch would stall the
            # window for as long as the repository takes to answer.
            self._commit_notes[rule_id] = (
                commit_subject(repo, event.new) or event.detail
            )
            self._commit_diff_ready.emit(
                rule_id, repo, commit_changes(repo, event.old, event.new)
            )

        threading.Thread(target=measure, name="commit-diff", daemon=True).start()

    def _on_commit_diff(self, rule_id: str, repo: str, changes: object) -> None:
        """git named the files one commit touched; send exactly those."""
        # Popped whichever way this goes: a note left behind would be shown
        # against the next commit to this rule, which is worse than none.
        note = self._commit_notes.pop(rule_id, "")
        rule = self._rule(rule_id)
        if rule is None or self._closing:
            return
        if not isinstance(changes, list):
            self._sync_now(rule)  # git could not say; compare everything
            return
        ignores = self._rule_ignores(rule)
        uploads, removals = self._commit_targets(rule, repo, changes, ignores)
        if not rule.delete_remote:
            removals = []
        log = self._activity()
        entry = self._activity_events.pop(rule_id, None)
        if entry is None:
            entry = log.log_event(f"Commit — {rule.name}")
        if not uploads and not removals:
            log.set_outcome(
                entry, "nothing in the commit touches this folder", kind="info"
            )
            self._set_status(
                f"{rule.name}: the commit changed nothing under this folder."
            )
            return
        if not self._connected:
            # The full comparison on reconnect covers however many commits
            # pile up while the connection is down.
            self._sync_pending.add(rule.id)
            self._activity_events[rule_id] = entry
            log.set_outcome(entry, "waiting for the connection…", kind="info")
            self._set_status(f"{rule.name}: will sync once the connection is up.")
            return
        what = (
            f"sync {len(uploads)} file(s) and {len(removals)} removal(s) "
            "from this commit to"
        )
        if not self._confirm_production(what):
            log.set_outcome(entry, "stopped at the production guard", kind="cancelled")
            return
        removing = bool(removals)
        if len(removals) > _BULK_REMOVAL:
            # A checkout or reset moves HEAD too, and can "delete" thousands
            # of files; that much removal is worth a question even here.
            removing = self._confirm_removals(rule, removals)
        if uploads:
            log.add_files(
                entry,
                [(path, local_relative(rule.local, path)) for path in uploads],
            )
            self._upload_tree(
                [(path, False) for path in uploads],
                rule.remote,
                flatten=rule.local,
                rules=ignores,
                quiet=True,
                origin=origin_with_note("git", note),
            )
        if removals:
            shown = removals[:20]
            if len(removals) > len(shown):
                shown = shown + [f"… and {len(removals) - len(shown)} more"]
            log.add_notes(
                entry,
                shown,
                outcome="removal requested" if removing else "left alone",
            )
        if removing:
            self._sync_delete_requested.emit(removals)
        parts = []
        if uploads:
            parts.append(f"uploading {len(uploads)} file(s)")
        if removing:
            parts.append(f"removing {len(removals)} item(s)")
        elif removals:
            parts.append(f"leaving {len(removals)} removal(s) alone")
        self._set_status(f"{rule.name}: " + ", ".join(parts) + ".")

    def _commit_plan(
        self, rule: SyncRule, repo: str, changes: list, ignores: IgnoreRules
    ) -> dict:
        """Everything one commit means for one rule, with the reasons kept.

        Filters the commit's file list down to what the rule owns and what
        ``ignores`` allows, and - unlike the counts that used to be all anyone
        saw - records the server path each file is going to and why the rest
        are staying put. Removals come back regardless of the rule's
        delete_remote flag; the caller decides whether to act on them.

        ``uploads`` are (local path, path within the folder, remote path),
        ``removals`` are (path within the folder, remote path), and ``skipped``
        are (path within the repository, why it is not going).
        """
        uploads: list[tuple[str, str, str]] = []
        removals: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        for status, rel in changes:
            path = os.path.normpath(os.path.join(repo, rel.replace("/", os.sep)))
            if not rule.owns(path):
                skipped.append((rel, "outside the folder on the left"))
                continue
            relative = rule.relative(path)
            if not relative:
                skipped.append((rel, "is the folder itself"))
                continue
            if ignores.is_ignored(relative):
                skipped.append((rel, "matched by .deployignore / .gitignore"))
                continue
            if status == "D":
                remote = rule.remote_for(path)
                if remote and remote != rule.remote:
                    removals.append((relative, remote))
                else:
                    skipped.append((rel, "deleted, but not a file this rule owns"))
            elif os.path.isfile(path):
                uploads.append((path, relative, rule.remote_for(path)))
            else:
                # A committed file already gone from disk again is the next
                # commit's (or the watcher's) business, not this one's.
                skipped.append((rel, "gone from disk again since the commit"))
        return {
            "uploads": uploads,
            "removals": removals,
            "skipped": skipped,
            "local": rule.local,
            "remote": rule.remote,
        }

    def _commit_targets(
        self, rule: SyncRule, repo: str, changes: list, ignores: IgnoreRules
    ) -> tuple[list[str], list[str]]:
        """(local uploads, remote removals) for one commit - the acting view."""
        plan = self._commit_plan(rule, repo, changes, ignores)
        return (
            [local for local, _rel, _remote in plan["uploads"]],
            [remote for _rel, remote in plan["removals"]],
        )

    # ----- offering to push commits nothing is syncing ---------------------
    # "I committed" is the strongest signal this app ever gets that a tree is
    # meant to go somewhere - waiting for the user to configure a sync first
    # meant the commits before that quietly went nowhere. So the repository
    # the local pane is browsing is watched even with no rule, and a commit
    # there offers itself: push once, push every commit, or stop asking.
    def _watch_pane_repo(self, path: str) -> None:
        """Keep one commit watcher on the repository the local pane is in."""
        repo = find_repo(path) or ""
        current = self._repo_watcher.repo if self._repo_watcher is not None else ""
        if repo == current:
            return
        # A different repository entirely: whatever the old one committed is
        # no longer what the pane is about, so the offer goes with it.
        if self._commit_notice_open:
            self._notice.hide_quietly()
        self._pending_commit = None
        self._commit_notice_open = False
        if self._repo_watcher is not None:
            self._repo_watcher.stop()
            self._repo_watcher = None
        if not repo:
            return
        watcher = GitCommitWatcher(repo, self._pane_commit.emit)
        if watcher.valid and watcher.start(prime=True):
            self._repo_watcher = watcher

    def _on_pane_commit(self, event: object) -> None:
        """The repository on show recorded a commit; consider offering a push."""
        if self._closing or not isinstance(event, CommitEvent):
            return
        if not self._connected or not self._local.path or not self._remote.path:
            return
        repo = self._repo_watcher.repo if self._repo_watcher is not None else ""
        if not repo:
            return
        # A folder the user has already made a sync decision about - any rule,
        # any mode, anywhere in this repository - is never nagged about. An
        # armed on-commit rule handles the push itself; a paused one is the
        # user having said "stop asking".
        for rule in self._sync_rules():
            if rule.covers(self._local.path):
                return
            if _inside(rule.local, repo) or _same_path(rule.local, repo):
                return

        def measure() -> None:
            self._pane_commit_diff.emit(
                repo, event, commit_changes(repo, event.old, event.new)
            )

        threading.Thread(target=measure, name="pane-commit-diff", daemon=True).start()

    def _on_pane_commit_diff(self, repo: str, event: object, changes: object) -> None:
        """Offer to push a commit made in a folder nothing is syncing yet."""
        if self._closing or not self._connected:
            return
        detail = event.describe() if isinstance(event, CommitEvent) else "HEAD moved"
        short = event.short if isinstance(event, CommitEvent) else ""
        if not isinstance(changes, list):
            self._set_status(f"Commit noticed ({detail}); use Sync to push it.")
            return
        # Remembered whatever the answer turns out to be, so "Push the last
        # commit" in the Sync menu still works once this notice is gone.
        self._pending_commit = {
            "repo": repo, "detail": detail, "short": short, "changes": changes,
        }
        self._last_commit = dict(self._pending_commit)
        self._commit_notice_open = True
        self._commit_notice_pair = None  # a new commit; nothing is up to date
        self._show_pane_commit_notice()

    def _pane_commit_plan(self) -> dict | None:
        """What the last noticed commit would do, for the panes as they are.

        There is no rule for this folder - that is the whole reason the offer
        exists - so the only pairing available is the one on screen. Working it
        out fresh each time is what keeps the strip honest: the folder it names
        is the folder the push uses, even after the panes have moved.
        """
        last = self._pending_commit or self._last_commit
        if not last or not self._local.path or not self._remote.path:
            return None
        changes = last.get("changes")
        if not isinstance(changes, list):
            return None
        repo = str(last.get("repo", ""))
        rule = self._pane_commit_rule()
        plan = self._commit_plan(rule, repo, changes, self._rule_ignores(rule))
        plan.update(
            repo=repo,
            repo_label=describe_repo(repo) or os.path.basename(repo),
            detail=str(last.get("detail", "")),
            short=str(last.get("short", "")),
        )
        return plan

    def _show_pane_commit_notice(self) -> None:
        """Say which two folders the offer is between, in so many words.

        Saying only "this would upload 3 files" left the destination to be
        guessed, and the guess - whichever folder happens to be open - is
        exactly the thing worth being told. So both paths are named, the
        pairing is admitted to be the panes, and files the commit touched that
        are *not* going anywhere are counted rather than quietly dropped.
        """
        if not self._commit_notice_open or self._closing or not self._connected:
            return
        pair = (self._local.path, self._remote.path)
        if pair == self._commit_notice_pair:
            return  # same two folders; a re-listing changes none of this
        self._commit_notice_pair = pair
        plan = self._pane_commit_plan()
        if plan is None:
            return
        self._refresh_commit_plan_dialog(plan)
        uploads, removals, skipped = (
            plan["uploads"], plan["removals"], plan["skipped"]
        )
        commit = plan["short"] or plan["detail"] or "HEAD"
        if not uploads and not removals:
            # Nothing of the commit lives under the folder on show. The offer
            # is not withdrawn - moving either pane brings it back - but there
            # is nothing to put on the strip in the meantime.
            self._notice.hide_quietly()
            self._set_status(
                f"Commit {commit}: nothing it changed is under {plan['local']}."
            )
            return
        parts = [f"upload {len(uploads)} file(s)"]
        if removals:
            parts.append(f"delete {len(removals)} file(s) from the server")
        where = plan["repo_label"]
        text = (
            f"Commit {commit}" + (f" in {where}" if where else "")
            + ": pushing it would " + " and ".join(parts)
            + f" — from {plan['local']} to {plan['remote']}, the two folders "
            "open in the panes."
        )
        if skipped:
            text += (
                f" {len(skipped)} other file(s) in the commit are not under that "
                "folder and stay where they are."
            )
        listing = "\n".join(
            [f"{rel}  →  {remote}" for _local, rel, remote in uploads[:60]]
            + [f"delete  {remote}" for _rel, remote in removals[:20]]
        )
        self._notice.show_notice(
            text,
            [
                ("Push", lambda: self._push_pane_commit(arm=False)),
                ("Push every commit", lambda: self._push_pane_commit(arm=True)),
                ("What goes where…", self._open_commit_plan),
            ],
            detail=listing,
            checkbox="Don't ask again",
            on_dismiss=self._remember_pane_answer,
            on_click=self._open_commit_plan,
        )

    # ----- the file-by-file view of an offer -------------------------------
    def _open_commit_plan(self) -> None:
        """Show every file the commit would send, and where each one lands."""
        plan = self._pane_commit_plan()
        if plan is None:
            self._set_status("No commit has been noticed in this folder yet.")
            return
        dialog = self._commit_plan_dialog(create=True)
        if dialog is None:
            return
        dialog.set_plan(plan)
        self._present(dialog)

    def _commit_plan_dialog(self, *, create: bool) -> CommitPlanDialog | None:
        dialog = self._dialogs.get("commit_plan")
        if isinstance(dialog, CommitPlanDialog):
            try:
                dialog.isVisible()  # probes that the C++ side is still there
                return dialog
            except RuntimeError:
                self._dialogs.pop("commit_plan", None)
        if not create:
            return None
        dialog = CommitPlanDialog(self._settings.dark_mode, self)
        dialog.push_requested.connect(
            lambda arm: self._push_pane_commit(arm=bool(arm))
        )
        self._dialogs["commit_plan"] = dialog
        return dialog

    def _refresh_commit_plan_dialog(self, plan: dict) -> None:
        """Keep an open plan window in step with the panes underneath it."""
        dialog = self._commit_plan_dialog(create=False)
        if dialog is None:
            return
        try:
            dialog.set_plan(plan)
        except RuntimeError:
            self._dialogs.pop("commit_plan", None)

    def _remember_pane_answer(self) -> None:
        """Closing the notice with "Don't ask again" ticked is an answer too."""
        self._commit_notice_open = False
        if not self._notice.remembered():
            return
        rule = self._pane_commit_rule()
        if not rule.local or not rule.remote:
            return
        self._sync_store.put(rule)  # paused: remembered, but never triggers
        self._apply_sync_marks()
        self._refresh_sync_dialog()
        self._set_status(
            f"{rule.name}: no more commit prompts for this folder. The Sync "
            "menu can still push it whenever you want."
        )

    def _pane_commit_rule(self) -> SyncRule:
        """A rule pairing the two directories on show, for a one-off push."""
        return SyncRule(
            profile_id=self._profile.id,
            local=self._local.path,
            remote=self._remote.path,
            mode=SyncMode.OFF,
        )

    def _push_pane_commit(self, *, arm: bool) -> None:
        """Act on the offer: push the noticed commit, arming the pair if asked.

        The commit is taken from what is remembered rather than from arguments
        bound when the strip was drawn, so pushing cannot send a different
        commit - or into a different folder - than the strip described.
        """
        self._commit_notice_open = False
        last = self._pending_commit or self._last_commit
        if not last:
            self._set_status("No commit has been noticed in this folder yet.")
            return
        repo = str(last.get("repo", ""))
        changes = list(last.get("changes") or [])
        rule = self._pane_commit_rule()
        if not rule.local or not rule.remote:
            return
        if arm:
            rule.mode = SyncMode.ON_COMMIT
        if arm or self._notice.remembered():
            # Stored even when paused: no more prompts, and the folder shows
            # up in the Sync menu ready to be armed properly later.
            rule = self._sync_store.put(rule)
            if arm:
                self._start_sync_rule(rule)
                self._set_status(
                    f"Syncing {rule.local} to {rule.remote} on each commit."
                )
            self._apply_sync_marks()
            self._refresh_sync_dialog()
        if self._sync_store.get(rule.id) is None:
            self._sync_transient[rule.id] = rule
        self._on_commit_diff(rule.id, repo, changes)

    def _push_last_commit(self) -> None:
        """Push the most recent commit again, from the Sync menu."""
        if not self._last_commit or not self._require_connection():
            self._set_status("No commit has been noticed in this folder yet.")
            return
        self._push_pane_commit(arm=False)

    # ----- publishing out of history --------------------------------------
    # Everything else this app deploys is whatever is on disk now, which is
    # right until the moment it is badly wrong: the bad release is live, the
    # fix is "put Friday's version back", and the working tree is three commits
    # past Friday. Doing that with git means checkout, deploy, checkout back -
    # three chances to leave the wrong thing somewhere. So the old bytes are
    # extracted to a scratch folder and uploaded from there. HEAD never moves
    # and the working tree is never touched.
    def _open_git_history(self) -> None:
        repo = find_repo(self._local.path)
        if not repo:
            self._set_status(
                f"{self._local.path} is not inside a git repository, so there "
                "is no history to read."
            )
            return
        dialog = self._dialogs.get("git_history")
        if isinstance(dialog, GitHistoryDialog):
            try:
                dialog.isVisible()
                self._present(dialog)
                return
            except RuntimeError:
                self._dialogs.pop("git_history", None)
        dialog = GitHistoryDialog(
            repo,
            remote=self._publish_rule().remote,
            dark=self._settings.dark_mode,
            parent=self,
        )
        dialog.publish_requested.connect(
            lambda sha, rels: self._publish_from_commit(repo, str(sha), rels)
        )
        self._dialogs["git_history"] = dialog
        self._present(dialog)

    def _publish_from_commit(self, repo: str, sha: str, rels: object) -> None:
        """Send some files as they were at ``sha``, wherever they belong now."""
        if not isinstance(rels, list) or not rels or not sha:
            return
        if not self._require_connection():
            return
        rule = self._publish_rule()
        if not rule.local or not rule.remote:
            self._set_status(
                "Open the folder to publish into on the right first — that "
                "pairing is where these files go."
            )
            return
        # Which of them this pairing can actually place, worked out before
        # anything is extracted: a file outside the folder on the left has
        # nowhere to land, and saying so now beats a silent short delivery.
        placeable = [
            rel
            for rel in rels
            if rule.owns(os.path.join(repo, rel.replace("/", os.sep)))
        ]
        if not placeable:
            self._set_status(
                f"None of those {len(rels)} file(s) sit under {rule.local}, so "
                "this folder pairing cannot place them."
            )
            return
        if not self._confirm_production(
            f"publish {len(placeable)} file(s) from commit {sha[:8]} to"
        ):
            return
        short = sha[:8]
        entry = self._activity().log_event(
            f"Publish — commit {short}",
            detail=f"{len(placeable)} file(s) from history → {rule.remote}",
        )
        self._publish_events[sha] = entry
        self._tool_progress.start(f"Reading commit {short}…")
        self._set_status(
            f"Extracting {len(placeable)} file(s) from {short} — your working "
            "copy is not touched."
        )
        dest = os.path.join(self._export_root, f"{short}-{uuid.uuid4().hex[:6]}")

        def extract() -> None:
            # git runs off the GUI thread: a big commit is several seconds.
            self._publish_notes[sha] = commit_subject(repo, sha)
            try:
                export = export_files(repo, sha, placeable, dest)
            except OSError as exc:
                export = Export(root=dest, files=[], failures=[("", str(exc))])
            self._commit_export_ready.emit(sha, rule.id, repo, export)

        threading.Thread(target=extract, name="git-export", daemon=True).start()

    def _publish_rule(self) -> SyncRule:
        """Where a file published from history lands.

        The rule that owns the folder on the left if there is one, because that
        is the pairing the user already configured; otherwise the two panes,
        which is the only other honest answer.
        """
        rule = self._sync_store.owner(self._profile.id, self._local.path)
        if rule is not None and rule.remote:
            return rule
        return self._pane_commit_rule()

    def _on_commit_export(
        self, sha: str, rule_id: str, repo: str, export: object
    ) -> None:
        """The old bytes are on disk; put them where the pairing says."""
        self._tool_progress.stop()
        if self._closing or not isinstance(export, Export):
            return
        log = self._activity()
        entry = self._publish_events.pop(sha, None)
        note = f"{sha[:8]} {self._publish_notes.pop(sha, '')}".strip()
        rule = self._rule(rule_id) or self._publish_rule()
        if not export.ok:
            reason = export.failures[0][1] if export.failures else "nothing to send"
            if entry is not None:
                log.set_outcome(entry, f"could not be read from git: {reason}")
            self._set_status(f"Nothing was published from {sha[:8]}: {reason}")
            return
        # The scratch tree mirrors the repository, so the rule's own path
        # arithmetic gives the right answer as long as it is asked about
        # the repository path rather than the scratch one.
        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for local_path, rel in export.files:
            target = RemoteFS.parent(
                rule.remote_for(os.path.join(repo, rel.replace("/", os.sep)))
            )
            by_dir.setdefault(target, []).append((local_path, False))
        origin = origin_with_note("publish", note)
        for target, group in by_dir.items():
            self._upload_quiet_requested.emit(
                (group, IgnoreRules.empty(), origin), target, True
            )
        if entry is not None:
            log.add_files(entry, list(export.files))
            if export.failures:
                log.add_notes(
                    entry,
                    [f"{rel}: {why}" for rel, why in export.failures[:20]],
                    outcome="not published",
                )
        missed = f", {len(export.failures)} skipped" if export.failures else ""
        self._set_status(
            f"Publishing {len(export.files)} file(s) from commit {sha[:8]} "
            f"to {rule.remote}{missed}."
        )

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
        log = self._activity()
        entry = self._activity_events.pop(rule_id, None)
        if entry is None:
            entry = log.log_event(
                f"Sync — {rule.name}", detail=f"compared with {rule.remote}"
            )
        if not uploads and not removals:
            log.set_outcome(
                entry,
                f"already in step (compared by {report.compared_by})",
                kind="info",
            )
            self._set_status(
                f"{rule.name}: already in step with {rule.remote} "
                f"(compared by {report.compared_by})."
            )
            return
        if uploads and not self._confirm_production(
            f"upload {len(uploads)} file(s) to"
        ):
            log.set_outcome(entry, "stopped at the production guard", kind="cancelled")
            return
        removing = bool(removals) and self._confirm_removals(rule, removals)
        if uploads:
            log.add_files(
                entry,
                [(path, local_relative(rule.local, path)) for path in uploads],
            )
            self._upload_tree(
                [(path, False) for path in uploads],
                rule.remote,
                flatten=rule.local,
                rules=self._rule_ignores(rule),
                quiet=True,
                origin="sync",
            )
        if removals:
            shown = removals[:20]
            if len(removals) > len(shown):
                shown = shown + [f"… and {len(removals) - len(shown)} more"]
            log.add_notes(
                entry,
                shown,
                outcome="removal requested" if removing else "left alone",
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
            # Folder statistics on a big directory can hold the tool channel
            # for well over the 20 seconds that 5 retries allowed, and a sync
            # dropped for that reason looked exactly like "git sync does not
            # work". A minute of patience costs nothing.
            if busy and attempts < 15 and not self._closing:
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
            entry = self._activity_events.pop(rule_id, None)
            if entry is not None:
                self._activity().set_outcome(entry, message, kind="failed")
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
        compare = menu.addAction(
            "Compare with the server (F9)", lambda: self._on_compare()
        )
        compare.setToolTip("Hash both sides and show exactly what differs")
        history = menu.addAction("Git history…", self._open_git_history)
        history.setToolTip(
            "Every commit in this repository, and the files from any of them - "
            "published as they were, without touching your working copy"
        )
        history.setEnabled(bool(find_repo(self._local.path)))
        last = self._last_commit
        if last:
            # The offer a commit made survives being dismissed: the commit is
            # remembered, so pushing it is still one click away afterwards.
            commit = last.get("short") or "HEAD"
            push_last = menu.addAction(
                f"Push the last commit ({commit})", self._push_last_commit
            )
            target = self._remote.path or "the folder open on the right"
            push_last.setToolTip(
                f"{last.get('detail', '')} - into {target}"
            )
            menu.addAction(
                f"What the last commit ({commit}) would send\u2026",
                self._open_commit_plan,
            ).setToolTip("Every file, and the server path each one lands on")
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
        menu.addAction("Sync activity…", self._open_sync_activity)

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
        dialog.remote_requested.connect(self._on_sync_edit_remote)
        dialog.local_requested.connect(self._on_sync_edit_local)
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

    # ----- the sync activity log -------------------------------------------
    # The watchers work in the background, so "did it see my commit, and did
    # everything go up?" needs somewhere to look. Events are logged into the
    # dialog whether or not it is on screen; opening it later shows the lot.
    def _activity(self, *, create: bool = True) -> SyncActivityDialog | None:
        dialog = self._dialogs.get("activity")
        if isinstance(dialog, SyncActivityDialog):
            try:
                dialog.isVisible()  # probes that the C++ side is still there
                return dialog
            except RuntimeError:
                self._dialogs.pop("activity", None)
        if not create:
            return None
        dialog = SyncActivityDialog(
            self._profile.label, dark=self._settings.dark_mode, parent=self
        )
        self._dialogs["activity"] = dialog
        return dialog

    def _open_sync_activity(self) -> None:
        self._present(self._activity())

    def _on_activity_item(self, item) -> None:
        """Feed upload outcomes to the activity log, when there is one."""
        dialog = self._activity(create=False)
        if dialog is not None and item.upload:
            dialog.update_transfer(item)

    # ----- work handed over by the MCP bridge -----------------------------
    # Everything Claude does to a server used to happen in the MCP process, on
    # its own connection, with a direct call - which is why none of it ever
    # appeared anywhere in this window. It arrives here instead and goes
    # through this tab's worker, so an upload becomes a queue row with a
    # shadow backup behind it, a delete is journalled for Undo and re-lists
    # the pane, and the caller is told what actually happened rather than that
    # it submitted something.
    #
    # Transfers are matched to their queue rows by direction and destination;
    # deletes and folder creation carry a request id, because op_done and
    # op_failed say what happened without saying which request it was about.
    def accept_bridge_upload(self, pairs, note: str, on_done) -> str:
        """Queue (local, remote) pairs for the MCP bridge.

        Returns "" once the work is queued - ``on_done`` is then called with a
        summary as soon as every file has reached a terminal state - or the
        reason it was not taken. The remote name has to match the local one:
        the queue derives it from the file it is sending, so an upload that
        also renames cannot be expressed here, and is better done by the
        caller than silently done wrong.
        """
        if not self._connected:
            return "the tab for that connection is not connected"
        wanted = []
        for local, remote in pairs:
            if not os.path.isfile(local):
                return f"{local} is not a file on this machine"
            if os.path.basename(remote) != os.path.basename(local):
                return "the upload renames the file, which the queue cannot do"
            wanted.append((local, remote))
        if not wanted:
            return "nothing to upload"

        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for local, remote in wanted:
            by_dir.setdefault(RemoteFS.parent(remote) or "/", []).append(
                (local, False)
            )
        self._track_bridge_transfer(
            [_transfer_key(True, remote) for _local, remote in wanted], on_done
        )
        # One queue for the whole handover, not one per sub-directory. A
        # folder push spread over thirty directories would otherwise drain and
        # restart the pool thirty times, re-listing the server between each -
        # which is the cost upload_groups exists to avoid.
        self._upload_groups_requested.emit(
            (
                list(by_dir.items()),
                IgnoreRules.empty(),
                origin_with_note("mcp", note),
            ),
            RemoteFS.parent(wanted[0][1]) or "/",
            True,
        )
        self._queue_btn.setChecked(True)
        self._set_status(
            f"Claude is uploading {len(wanted)} file(s) to {self._profile.label}."
        )
        return ""

    def accept_bridge_download(self, pairs, note: str, on_done) -> str:
        """Queue (remote, local) pairs for the MCP bridge.

        The mirror of accept_bridge_upload, and it refuses for the mirror
        reason: the queue names what it fetches after the file on the server,
        so a download that renames on the way cannot be expressed here.
        """
        if not self._connected:
            return "the tab for that connection is not connected"
        wanted = []
        for remote, local in pairs:
            if RemoteFS.basename(remote) != os.path.basename(local):
                return "the download renames the file, which the queue cannot do"
            wanted.append((remote, os.path.abspath(local)))
        if not wanted:
            return "nothing to download"

        by_dir: dict[str, list[tuple[str, bool]]] = {}
        for remote, local in wanted:
            by_dir.setdefault(os.path.dirname(local), []).append((remote, False))
        for local_dir in by_dir:
            try:
                os.makedirs(local_dir, exist_ok=True)
            except OSError as exc:
                return f"cannot write to {local_dir}: {exc}"
        self._track_bridge_transfer(
            [_transfer_key(False, local) for _remote, local in wanted], on_done
        )
        self._download_groups_requested.emit(
            (
                list(by_dir.items()),
                IgnoreRules.empty(),
                origin_with_note("mcp", note),
            )
        )
        self._queue_btn.setChecked(True)
        self._set_status(
            f"Claude is downloading {len(wanted)} file(s) from "
            f"{self._profile.label}."
        )
        return ""

    def accept_bridge_delete(self, path: str, is_dir: bool, on_done) -> str:
        """Delete one path on the server for the MCP bridge.

        Worth routing here more than any of the others: this is the one
        destructive act with an undo, and the undo lives in this tab's
        journal. A delete done in the MCP process could not be put back.
        """
        if not self._connected:
            return "the tab for that connection is not connected"
        if not path or path.rstrip("/") in ("", "/"):
            return "that path is the root"
        request_id = self._track_bridge_op(on_done)
        # Passed through as-is, None included: bool(None) is False, which
        # would send a directory to be removed as though it were a file and
        # throw away the whole point of letting the worker stat it.
        self._bridge_delete_requested.emit(request_id, [(path, is_dir)])
        self._set_status(f"Claude is deleting {path} on {self._profile.label}.")
        return ""

    def accept_bridge_mkdir(self, path: str, on_done) -> str:
        """Create a folder on the server for the MCP bridge."""
        if not self._connected:
            return "the tab for that connection is not connected"
        if not path:
            return "no path was given"
        request_id = self._track_bridge_op(on_done)
        self._bridge_mkdir_requested.emit(request_id, path)
        return ""

    # ----- keeping track of both kinds ------------------------------------
    def _track_bridge_transfer(self, keys, on_done) -> None:
        self._bridge_jobs.append(
            {
                "pending": set(keys),
                "done": 0,
                "failed": [],
                "on_done": on_done,
                "total": len(keys),
            }
        )

    def _track_bridge_op(self, on_done) -> str:
        request_id = uuid.uuid4().hex[:12]
        self._bridge_ops[request_id] = on_done
        return request_id

    def _on_bridge_item(self, item) -> None:
        """Tick one file off whichever bridge job was waiting for it."""
        if not self._bridge_jobs or not item.state.finished:
            return
        key = _transfer_key(item.upload, item.destination)
        for job in list(self._bridge_jobs):
            if key not in job["pending"]:
                continue
            job["pending"].discard(key)
            if item.state is JobState.DONE:
                job["done"] += 1
            else:
                reason = item.error or item.state.value
                job["failed"].append(f"{item.destination}: {reason}")
            if not job["pending"]:
                self._bridge_jobs.remove(job)
                self._finish_bridge_job(job)
            return

    def _on_bridge_op(self, request_id: str, ok: bool, message: str) -> None:
        """Answer the caller waiting on one delete or folder creation."""
        callback = self._bridge_ops.pop(request_id, None)
        if callback is None:
            return
        callback(
            {"ok": bool(ok), "detail": message, "error": "" if ok else message}
        )
        self._set_status(message)

    def _finish_bridge_job(self, job: dict, abandoned: str = "") -> None:
        """Tell the waiting MCP call how its files got on."""
        callback = job.get("on_done")
        if callback is None:
            return
        job["on_done"] = None  # answer once, whatever else happens
        callback(
            {
                "ok": True,
                "sent": job["done"],
                "total": job["total"],
                "failed": list(job["failed"]),
                "abandoned": abandoned,
            }
        )

    def _abandon_bridge_jobs(self, reason: str) -> None:
        """Answer anything still waiting when this tab stops being able to.

        Without this the caller waits out its whole timeout on a connection
        that has already gone - minutes of silence where one sentence would
        do.
        """
        jobs, self._bridge_jobs = self._bridge_jobs, []
        for job in jobs:
            self._finish_bridge_job(job, abandoned=reason)
        waiting, self._bridge_ops = self._bridge_ops, {}
        for callback in waiting.values():
            callback({"ok": False, "error": reason})

    # ----- commands -------------------------------------------------------
    def _set_active(self, *, remote: bool) -> None:
        self._remote_active = remote
        # Which pane you are in decides which transfer is the offered one.
        self._refresh_actions()

    def _active_pane(self) -> _FilePane:
        return self._remote if self._remote_active else self._local

    def _on_browse_local(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a local directory", self._local.path
        )
        if chosen:
            self._load_local(chosen)

    def _on_browse_remote(self) -> None:
        """Pick a folder on the server without walking the pane there first.

        A fresh dialog every time: it is aimed at wherever the pane is now, and
        a stale tree from three folders ago is worse than no tree at all.
        """
        if not self._require_connection():
            return
        self._open_folder_picker(
            self._remote.path or "/",
            f"Folders on {self._profile.label}.",
            self._list_remote,
        )

    def _open_folder_picker(self, start: str, label: str, on_chosen) -> None:
        """Open the server's folder tree at ``start`` and report the answer.

        One dialog at a time, under the key the listing callbacks look up. A
        fresh one every time: it is aimed at wherever it was asked to start,
        and a stale tree from three folders ago is worse than no tree at all.
        """
        stale = self._dialogs.pop("folders", None)
        if stale is not None:
            try:
                stale.close()
                stale.deleteLater()  # parented here, so closing alone keeps it
            except RuntimeError:
                pass  # its C++ side has already gone
        dialog = RemoteFolderDialog(
            start or "/",
            label=label,
            dark=self._settings.dark_mode,
            parent=self,
        )
        dialog.folders_requested.connect(self._folders_requested.emit)
        dialog.chosen.connect(on_chosen)
        self._dialogs["folders"] = dialog
        self._present(dialog)
        dialog.start()  # connected first, so the answer for "/" has a home

    def _on_folders_listed(self, path: str, names: object) -> None:
        dialog = self._dialogs.get("folders")
        if not isinstance(dialog, RemoteFolderDialog) or not isinstance(names, list):
            return
        try:
            dialog.add_folders(path, names)
        except RuntimeError:
            self._dialogs.pop("folders", None)

    def _on_folders_failed(self, path: str, message: str) -> None:
        dialog = self._dialogs.get("folders")
        if isinstance(dialog, RemoteFolderDialog):
            try:
                dialog.show_error(path, message)
                return
            except RuntimeError:
                self._dialogs.pop("folders", None)
        self._set_status(f"{path}: {message}")

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
        rules = self._manual_rules(items)
        self._upload_requested.emit((items, rules), target)

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
        rules = self._manual_rules(items)
        self._upload_requested.emit((items, rules), target)

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
        rules = self._manual_rules(items)
        self._upload_requested.emit((items, rules), self._remote.path or "/")

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

    def _on_mkfile(self) -> None:
        """Create an empty file in whichever pane is active."""
        side = "remote" if self._remote_active else "local"
        name, ok = QInputDialog.getText(
            self, "New file", f"Name of the new {side} file:"
        )
        name = name.strip().replace("\\", "/").strip("/")
        if not ok or not name:
            return
        if self._remote_active:
            if not self._require_connection():
                return
            if not self._confirm_production(f"create {name} on"):
                return
            self._mkfile_requested.emit(
                RemoteFS.join(self._remote.path or "/", name)
            )
            return
        target = os.path.join(self._local.path, name.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(target) or self._local.path, exist_ok=True)
            # "x": never truncate a file that is already there. Creating one
            # is not the same request as emptying one.
            with open(target, "x"):
                pass
        except FileExistsError:
            self._set_status(f"{name} is already there; it was left alone.")
            return
        except OSError as exc:
            QMessageBox.warning(self, "Could not create file", str(exc))
            return
        self._load_local(self._local.path)
        self._set_status(f"Created {name}.")

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
            self._delete_requested.emit(
                [
                    (self._remote_child(name), self._deletes_as_tree(name, is_dir))
                    for name, is_dir in selection
                ]
            )
        else:
            self._delete_local(selection)

    def _deletes_as_tree(self, name: str, is_dir: bool) -> bool:
        """Whether this entry should be removed as a directory.

        A symlink pointing at a directory lists as a directory, and following
        one would empty whatever it points at while leaving the link behind.
        Deleting a link means deleting the link, so it goes as a file - which
        the pane can tell us, because the listing already knows.
        """
        if not is_dir:
            return False
        entry = self._remote.entry(name)
        return not (entry is not None and entry.is_link)

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
                self._remote.path or "/",
                history=self._shell_history.entries(),
                parent=self,
            )
            dialog.command_requested.connect(self._on_run_command)
            self._dialogs["exec"] = dialog
        dialog.show()
        dialog.raise_()

    def _on_run_command(self, command: str, cwd: str) -> None:
        self._shell_history.add(command)
        if self._is_production and self._settings.production_guard:
            if not self._confirm_production(f"run “{command}” on"):
                return
        self._exec_requested.emit(command, cwd)

    def _show_exec(self, payload: object) -> None:
        dialog = self._dialogs.get("exec")
        if isinstance(dialog, CommandBar):
            dialog.show_result(payload)
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
        # Without this a disabled entry can only shrug; with it, it says why.
        menu.setToolTipsVisible(True)
        has_shell = Capability.EXEC in self._capabilities

        one_file = len(selection) == 1 and not selection[0][1]
        editor = self._editor()
        editor_name = self._editor_name()
        if remote:
            menu.addAction("Download", self._on_download).setEnabled(bool(selection))
            menu.addAction(
                "Edit locally",
                lambda: self._on_edit_remote(selection[0][0]),
            ).setEnabled(one_file)
            in_editor = menu.addAction(
                f"Open in {editor_name}",
                lambda: self._open_remote_in_editor(selection[0][0]),
            )
            in_editor.setEnabled(one_file and editor is not None)
            if editor is None:
                in_editor.setToolTip(_NO_EDITOR_HINT)
            elif not one_file:
                in_editor.setToolTip(
                    "Pick a single file. A folder goes to the entry below."
                )
            else:
                in_editor.setToolTip(
                    f"Downloads {selection[0][0]} and uploads every save back "
                    "to the server while this tab is open."
                )
            folder_in_editor = menu.addAction(
                f"Open this folder in {editor_name} over SSH",
                self._open_remote_folder_in_editor,
            )
            blocked = _NO_EDITOR_HINT if editor is None else self._remote_ssh_block()
            if blocked:
                folder_in_editor.setEnabled(False)
                folder_in_editor.setToolTip(blocked)
            else:
                folder_in_editor.setToolTip(
                    f"{editor_name} edits the files where they are, over its "
                    "own SSH session - nothing is downloaded. Needs its "
                    "Remote-SSH extension."
                )
            menu.addSeparator()
        else:
            menu.addAction("Upload", self._on_upload).setEnabled(bool(selection))
            menu.addAction(
                "Open", lambda: self._on_open_local(selection[0][0])
            ).setEnabled(one_file)
            menu.addSeparator()

        menu.addAction("New folder (F7)", self._on_mkdir)
        menu.addAction("New file (Shift+F4)", self._on_mkfile)
        menu.addAction("Rename (F2)", self._on_rename).setEnabled(len(selection) == 1)
        menu.addAction("Delete (Del)", self._on_delete).setEnabled(bool(selection))
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
            menu.addSeparator()
            menu.addAction(
                "Start here next time", lambda: self._set_default_folder(remote=True)
            ).setToolTip(
                "Open this folder automatically whenever this connection is "
                "opened"
            )
            menu.addSeparator()
            menu.addAction("Compare with the server (F9)", lambda: self._on_compare())
        else:
            folder = self._selected_local_dir()
            synced = self._sync_store.find(self._profile.id, folder)
            sync_menu = menu.addMenu(
                "Sync folder" if synced is None else f"Sync folder ({synced.mode.label})"
            )
            self._fill_sync_menu(sync_menu)
            self._fill_ignore_menu(menu, selection)
            menu.addSeparator()
            menu.addAction(
                "Start here next time", lambda: self._set_default_folder(remote=False)
            ).setToolTip(
                "Open this folder automatically whenever this connection is "
                "opened"
            )
            menu.addAction("Open in Explorer", self._open_in_explorer)
            local_in_editor = menu.addAction(
                f"Open in {editor_name}", self._open_local_in_editor
            )
            local_in_editor.setEnabled(editor is not None)
            local_in_editor.setToolTip(
                _NO_EDITOR_HINT
                if editor is None
                else "Opens what is selected, or this folder when nothing is."
            )
            menu.addAction("Copy path", self._copy_local_path)
            menu.addSeparator()
            menu.addAction("Compare with the server (F9)", lambda: self._on_compare())
            git = menu.addAction("Git history…", self._open_git_history)
            git.setEnabled(bool(find_repo(self._local.path)))

        menu.exec(position)

    # ----- excluding things from deploys ----------------------------------
    # The rules that keep node_modules and .env off a server are a text file
    # everybody knows the syntax of and nobody wants to open mid-deploy. The
    # decision, though, is always made *looking at the file* - "not that one" -
    # so the menu on the file is where it belongs, and the rule it writes is
    # anchored to the exact path rather than the bare name, so excluding
    # /config/db.php does not also silence a db.php somewhere else.
    def _ignore_root(self) -> str:
        """The folder whose .deployignore governs what the local pane sends.

        The rules that a transfer actually consults come from the folder the
        sync rule names, or - with no rule - from the folder on show. Writing
        anywhere else produces a file that looks right and changes nothing.
        """
        rule = self._sync_store.owner(self._profile.id, self._local.path)
        if rule is not None and os.path.isdir(rule.local):
            return rule.local
        return self._local.path

    def _fill_ignore_menu(self, menu, selection: list[tuple[str, bool]]) -> None:
        """The "never deploy this" entries for the current local selection."""
        root = self._ignore_root()
        submenu = menu.addMenu("Never deploy")
        if not selection:
            submenu.setEnabled(False)
            return
        folders = sum(1 for _name, is_dir in selection if is_dir)
        files = len(selection) - folders
        if len(selection) == 1:
            name, is_dir = selection[0]
            what = f"“{name}” and everything in it" if is_dir else f"“{name}”"
        else:
            parts = []
            if folders:
                parts.append(f"{folders} folder(s) and everything in them")
            if files:
                parts.append(f"{files} file(s)")
            what = " and ".join(parts)
        exact = submenu.addAction(f"Add {what} to .deployignore")
        exact.triggered.connect(lambda: self._ignore_selection(by_name=False))
        if files:
            names = submenu.addAction(
                "Add by name, anywhere in the tree"
                if len(selection) > 1
                else f"Add every file named “{selection[0][0]}”"
            )
            names.triggered.connect(lambda: self._ignore_selection(by_name=True))
            names.setToolTip(
                "An unanchored rule: it also excludes files of that name in "
                "any subfolder"
            )
        submenu.addSeparator()
        show = submenu.addAction("Open .deployignore")
        show.triggered.connect(self._open_ignore_file)
        show.setToolTip(
            f"{ignore_file_path(root)} — the rules this folder's transfers use"
        )
        if os.path.normcase(root) != os.path.normcase(self._local.path):
            note = submenu.addAction(f"Rules live in {root}")
            note.setEnabled(False)

    def _ignore_selection(self, *, by_name: bool) -> None:
        """Write rules for the selected rows into the governing .deployignore."""
        selection = self._local.selection()
        if not selection:
            return
        root = self._ignore_root()
        if not root or not os.path.isdir(root):
            self._set_status("There is no local folder to write rules for.")
            return
        patterns: list[str] = []
        for name, is_dir in selection:
            full = self._local_child(name)
            if by_name and not is_dir:
                patterns.append(name)
                continue
            pattern = pattern_for(root, full, is_dir=is_dir)
            if pattern:
                patterns.append(pattern)
            else:
                # Outside the folder the rules govern: an anchored rule for it
                # would silently match nothing.
                self._set_status(
                    f"{full} is not inside {root}, so a rule there would not "
                    "apply to it."
                )
        if not patterns:
            return
        try:
            path, added = add_patterns(root, patterns)
        except OSError as exc:
            self._set_status(f"Could not write {root}\\.deployignore: {exc}")
            return
        if not added:
            self._set_status(f"Already in {path}: {', '.join(patterns)}")
            return
        self._set_status(
            f"Added {', '.join(added)} to {path}. "
            "Syncs, batch uploads and comparisons skip them from now on."
        )
        # The watcher and any armed rule read the file when they start, so the
        # ones running now have to be given the new rules.
        self._restart_ignore_consumers()

    def _restart_ignore_consumers(self) -> None:
        """Re-read the ignore rules everything currently watching is using."""
        if self._watcher is not None:
            self._restart_watcher(self._local.path)
        for rule_id in list(self._sync_watchers):
            rule = self._rule(rule_id)
            if rule is not None:
                self._start_sync_rule(rule)

    def _open_ignore_file(self) -> None:
        """Show the rules themselves, creating the file if it is not there."""
        root = self._ignore_root()
        path = ignore_file_path(root) if root else ""
        if not path:
            return
        if not os.path.exists(path):
            try:
                open(path, "a", encoding="utf-8").close()
            except OSError as exc:
                self._set_status(f"Could not create {path}: {exc}")
                return
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self._set_status(f"Opened {path}")

    def _set_default_folder(self, *, remote: bool) -> None:
        """Remember the folder in view as where this connection opens.

        Every session with a server starts in the same two places, and getting
        there was four clicks that nobody should have to repeat. The folder a
        single selected directory names wins over the one on show, because
        right-clicking a folder and being sent to its parent would be a lie.
        """
        pane = self._remote if remote else self._local
        selection = pane.selection()
        if len(selection) == 1 and selection[0][1]:
            target = (
                self._remote_child(selection[0][0])
                if remote
                else self._local_child(selection[0][0])
            )
        else:
            target = pane.path
        if not target:
            self._set_status("There is no folder here to remember.")
            return
        if remote:
            self._profile.remote_dir = target
            side = "The server side"
        else:
            self._profile.local_dir = target
            side = "This machine's side"
        self.profile_changed.emit(self._profile)
        self._set_status(
            f"{side} of {self._profile.label} will open at {target} from now on."
        )

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

    def _manual_rules(self, items) -> IgnoreRules:
        """The ignore rules to use for an upload somebody picked by hand.

        The ignore list keeps junk out of a bulk push - node_modules, caches,
        build output, and .env, which usually holds secrets that should not be
        copied over a server's own. For anything automatic that is right, and a
        commit-driven sync must go on obeying it: quietly overwriting a live
        .env with a developer's local one is a genuinely bad afternoon.

        Applied to a file someone selected and pressed Upload on, it is simply
        wrong. Picking the file *is* the decision, and there is nobody else to
        ask. It also failed in the worst possible way - filtered out before a
        queue was ever built, so there was no queue entry, no warning and no
        error, and .env just never went anywhere.

        So every entry named here goes, whatever the rules say. Walking *into*
        a selected folder still filters, so dragging a project folder across
        does not drag node_modules with it.
        """
        rules = self._ignore_rules()
        names = [
            os.path.basename(path.rstrip("\\/")) or path for path, _ in items
        ]
        held = [
            name
            for name, (_path, is_dir) in zip(names, items)
            if rules.is_ignored(name, is_dir=is_dir)
        ]
        if held:
            # Worth saying out loud - not worth a dialog in the way.
            shown = ", ".join(held[:4])
            if len(held) > 4:
                shown += f", and {len(held) - 4} more"
            subject = "is" if len(held) == 1 else "are"
            self._set_status(
                f"Sending {shown} — normally {subject} held back by the ignore "
                "rules, but you picked it."
            )
        return rules.allowing(names)

    def _confirm_production(self, action: str) -> bool:
        """Ask before anything destructive on a production connection.

        The warning can be switched off from inside itself, per connection:
        someone deploying to the same live site all day is answering the same
        question dozens of times, and a prompt answered by reflex has stopped
        being a safeguard. Settings can turn it back on for everything.
        """
        if not self._is_production or not self._settings.production_guard:
            return True
        if self._profile.id in self._settings.production_guard_off:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("PRODUCTION")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"You are about to {action} {self._profile.label}, which is marked "
            "as production.\n\nGo ahead?"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        remember = QCheckBox("Don't ask again for this connection")
        remember.setToolTip(
            "Uploads to this server stop asking. Settings ▸ Ask before "
            "production changes turns it back on for every connection."
        )
        box.setCheckBox(remember)
        confirmed = box.exec() == QMessageBox.StandardButton.Yes
        if confirmed and remember.isChecked():
            self._settings.production_guard_off.append(self._profile.id)
            self._settings.save()
            self._set_status(
                f"{self._profile.label}: production warnings are off for this "
                "connection."
            )
        return confirmed

    # ----- saying what will happen before it does --------------------------
    # The hardest thing about a two-pane transfer window is that nothing on it
    # says which way anything is about to go. Every control looks alike, so a
    # new user presses whichever word they recognise - and on a production
    # server the first word people recognise turns out to be "Compare".
    #
    # So the pair of transfer buttons is treated as one question with one
    # answer: whichever pane you are working in decides which of them is *the*
    # action, that one is the only loud control on screen, and it says out loud
    # what pressing it would do - how many items, and into which folder. A
    # button that cannot be pressed keeps its place and explains itself rather
    # than disappearing, so the layout never moves under anyone.
    def _refresh_actions(self) -> None:
        """Point the transfer pair at what is actually selected, and say so."""
        local = self._local.selection()
        remote = self._remote.selection()
        # The active pane decides, except when only the other one has anything
        # picked - then the user has already answered by selecting.
        uploading = not self._remote_active
        if local and not remote:
            uploading = True
        elif remote and not local:
            uploading = False
        elif not local and not remote:
            # Nothing picked anywhere, so nothing has been said yet. Offer
            # uploading: this is a deployment tool, and the remote pane being
            # the one that happens to hold focus at startup is not a reason to
            # point the loud button at the user's own disk.
            uploading = True

        # A changed-files list with something ticked in it is the action on
        # this screen. The footer pair steps back to secondary rather than
        # offering a second filled button, which by the rule this whole theme
        # is built on would be no louder than none.
        deferring = self._changes_panel.isVisible() and bool(
            self._changes_panel.selected()
        )

        self._dress_action(
            self._upload_btn,
            primary=uploading and not deferring,
            count=len(local),
            arrow="▲",
            verb="Upload",
            target=self._remote.path or "the server",
            empty="Pick files on the left to upload",
            blocked="" if self._connected else "Not connected yet",
        )
        self._dress_action(
            self._download_btn,
            primary=not uploading and not deferring,
            count=len(remote),
            arrow="▼",
            verb="Download",
            target=self._local.path or "this machine",
            empty="Pick files on the right to download",
            blocked="" if self._connected else "Not connected yet",
        )

    def _dress_action(
        self, button, *, primary: bool, count: int, arrow: str, verb: str,
        target: str, empty: str, blocked: str,
    ) -> None:
        """Give one transfer button its label, its tooltip and its weight."""
        role = "primary" if primary else "secondary"
        if button.objectName() != role:
            button.setObjectName(role)
            # A stylesheet is matched when the name is set, so a widget that
            # changes role has to be repolished or it keeps the old look.
            button.style().unpolish(button)
            button.style().polish(button)
        enabled = bool(count) and not blocked
        button.setEnabled(enabled)
        if not count:
            button.setText(f"{arrow} {verb}")
            button.setToolTip(blocked or empty)
            return
        button.setText(f"{arrow} {verb} {count}")
        button.setToolTip(
            blocked
            or f"{verb} {count} item(s) into {target}"
        )

    def _set_connection_state(self, state: str, text: str = "") -> None:
        """The pill in the corner: connecting, connected, or not.

        Connection state used to live only in the status line, mixed in with
        every other message and overwritten by the next one - so "why is
        nothing happening?" had no answer on screen. One pill, always in the
        same place, in the one colour that says which of three things is true.
        """
        labels = {
            "busy": "Connecting…",
            "ok": "Connected",
            "fail": "Not connected",
        }
        pill = self._state_pill
        pill.setText(labels.get(state, state))
        pill.setToolTip(text or labels.get(state, ""))
        if pill.property("state") != state:
            pill.setProperty("state", state)
            pill.style().unpolish(pill)
            pill.style().polish(pill)

    # ----- status ---------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self._status.setText(message)

    # ----- teardown -------------------------------------------------------
    def cleanup(self) -> None:
        """Cancel any transfer, close the connection, stop the thread."""
        self._closing = True
        self._abandon_bridge_jobs("the tab was closed before it finished")
        self._edit_timer.stop()
        self._edit_watch.clear()
        self._edit_editors.clear()
        # Scratch copies of edited files; the editor may still hold one open,
        # in which case it stays until Windows cleans the temp directory.
        shutil.rmtree(self._edit_root, ignore_errors=True)
        shutil.rmtree(self._export_root, ignore_errors=True)
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._repo_watcher is not None:
            self._repo_watcher.stop()
            self._repo_watcher = None
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
        if not self._thread.wait(3000):
            _abandon_thread(self._thread, self._worker)


#: Worker threads that outlived the tab that owned them. See _abandon_thread.
_ABANDONED: set = set()


def _abandon_thread(thread, worker) -> None:
    """Let a thread that will not stop finish in its own time, safely.

    Closing a tab while it is still connecting is the case that matters. The
    worker is inside a blocking connect - a wrong host takes as long as the TCP
    timeout - so the quit posted to its event loop is not looked at until that
    returns, and the three-second wait gives up. What happened next was fatal
    rather than untidy: the QThread is parented to the tab, so deleting the tab
    deleted a *running* QThread, and Qt answers that by aborting the process.
    Dropping the last Python reference to the worker at the same moment did the
    same thing to a QObject another thread was still executing.

    So the pair is cut loose instead: reparented out of the widget, kept alive
    here, and dropped once the thread really does finish. The connection
    attempt runs to its timeout on a thread nobody is waiting for, which costs
    one socket for a few seconds and crashes nothing.
    """
    try:
        thread.setParent(None)
    except RuntimeError:
        return
    pair = (thread, worker)
    _ABANDONED.add(pair)

    def done() -> None:
        _ABANDONED.discard(pair)
        try:
            # The queued close never ran - quit() beat it to the event loop -
            # so the socket the connect finally opened is closed from here.
            # The worker's thread has finished, so nothing else is touching it.
            worker.close_connection()
        except Exception:
            pass
        try:
            worker.deleteLater()
            thread.deleteLater()
        except RuntimeError:
            pass

    thread.finished.connect(done)
    # It may have finished between the wait timing out and the connection
    # above being made, in which case nothing would ever fire.
    if thread.isFinished():
        done()


def _missing_local_reason(target: str) -> str:
    """Why a local directory is not there - naming the usual culprit.

    A mapped network drive that Explorer shows but this app cannot see is
    almost always elevation: mapped drives belong to the unelevated logon
    session, so running as administrator hides them completely. Saying so is
    the difference between a five-second fix and concluding the app is broken.
    """
    drive = mapped_drive_letter(target)
    if drive and running_elevated() and not os.path.isdir(drive + "\\"):
        return (
            f"{target} is not visible because Sitekeeper is running as "
            f"administrator, and Windows hides mapped network drives like "
            f"{drive} from elevated programs. Start it normally (without "
            f"'Run as administrator'), or use the \\\\server\\share path "
            "instead, which works either way."
        )
    return f"{target} is not a directory."


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


def _spec_for(profile: ServerProfile, jump: object = None) -> ConnectionSpec:
    """Everything the worker thread needs to open this connection.

    ``jump`` is resolved by whoever has the vault open - a spec crosses
    threads, so it holds a plain JumpHost rather than the id of a profile it
    would have to look up.
    """
    return ConnectionSpec(
        kind=profile.kind,
        host=profile.host,
        port=profile.effective_port,
        username=profile.username,
        password=profile.password,
        private_key_path=profile.private_key_path,
        passive=profile.passive,
        use_agent=profile.use_agent,
        use_default_keys=profile.use_default_keys,
        host_key_mode=hostkeys.PROMPT,
        jump=jump,
        proxy_command=profile.proxy_command,
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


def _same_path(a: str, b: str) -> bool:
    """Whether two local paths name the same directory (case-insensitively)."""
    if not a or not b:
        return False
    return (
        os.path.normcase(os.path.normpath(a)).rstrip("\\/")
        == os.path.normcase(os.path.normpath(b)).rstrip("\\/")
    )


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
    """How one entry is shown: links with their target, the rest plain.

    Folders used to be bracketed; the folder icon says it now.
    """
    label = entry.name
    if entry.is_link:
        label = f"{label} →" + (f" {entry.link_target}" if entry.link_target else "")
    return label


# ----- formatting helpers -------------------------------------------------
def _transfer_key(upload: bool, destination: str) -> tuple[bool, str]:
    """Name one queued transfer the way both sides of the bridge will agree.

    Keyed on the destination rather than the source, because that is what
    identifies the job: two uploads of different local files to the same
    remote path are the same piece of work arriving twice, and the queue
    reports the destination either way. Direction is part of the key so a
    download to ``C:/x/a.php`` cannot be ticked off by an upload of it.
    """
    if upload:
        return (True, "/" + str(destination).replace("\\", "/").strip("/"))
    return (False, os.path.normcase(os.path.abspath(str(destination))))


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
