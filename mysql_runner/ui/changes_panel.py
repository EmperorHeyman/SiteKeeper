"""What Watch has seen change, and what you want to do about it.

Ticking "Watch" used to do one of two things, and neither of them was what the
checkbox implies. With auto-upload off - the default - it set a status line
naming three files, which the next message wiped: you were told something
changed, once, in passing, and then it was gone. With auto-upload on it sent
everything the moment it settled, which is a fine mode to be in and a terrible
one to be in by accident, because nothing ever asked.

Watching is a question, so this is the answer: a list that accumulates. Every
file the watcher has noticed since you ticked the box stays here until you send
it or clear it, with what happened to it and when. Files are grouped under
their folder and the folders are tri-state, so "push this whole directory and
nothing else" is one click - which is the thing the old status line could never
express, and the reason "pick files and whole folders" had to mean folders too.

Added and modified files arrive ticked, because a watcher that noticed your
save is nearly always a watcher whose file you meant to send. Deletions arrive
shown but not selectable: mirroring a removal onto a server is a different and
much less forgiving act than sending a file, and it belongs to a folder sync
rule with a defined scope rather than to a panel that watches wherever the
local pane happens to point. The count in the header says how many there are so
they cannot be missed.
"""

from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.transfer.watcher import Change, ChangeKind
from mysql_runner.ui import theme
from mysql_runner.ui.remote_tools import human_size

_COLUMNS = ("File", "Change", "Size", "When")

#: The Change this row stands for.
_CHANGE_ROLE = Qt.ItemDataRole.UserRole + 1
#: Set on the folder rows, so recounting can skip them without guessing.
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 2
#: The folder row's key in _groups. Held separately from the text because the
#: root group's key is "" and its text is not.
_DIR_ROLE = Qt.ItemDataRole.UserRole + 3

#: The deletions group's key in _groups. Angle brackets are not legal in a
#: Windows path component, so this can never collide with a real directory.
_DELETED_KEY = "<deleted>"

#: How many changes the list holds before the oldest fall off. A watcher
#: pointed at a build directory can produce thousands, and a list that long
#: has stopped being something anyone reads.
MAX_ROWS = 2000

_KIND_TEXT = {
    ChangeKind.ADDED: "added",
    ChangeKind.MODIFIED: "modified",
    ChangeKind.REMOVED: "deleted locally",
}


class ChangesPanel(QWidget):
    """The running list of local changes, and the choice of what to send."""

    #: The files ticked when Upload was pressed, as Change objects.
    upload_requested = pyqtSignal(object)
    #: One local path to show in the file manager.
    reveal_requested = pyqtSignal(str)
    #: How many changes are listed. Carried onto the tab's button, so a tab
    #: that has noticed something says so while the list is closed.
    count_changed = pyqtSignal(int)
    #: How many are ticked. The tab uses this to decide which button on the
    #: screen is the loud one - see FileManagerTab._refresh_actions.
    selection_changed = pyqtSignal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("changespanel")
        #: rel path -> its row, so a file saved five times is still one row.
        self._rows: dict[str, QTreeWidgetItem] = {}
        #: rel dir -> the folder row holding it.
        self._groups: dict[str, QTreeWidgetItem] = {}
        #: What the top row is called. The watched folder's own name, so the
        #: tree reads as a path from somewhere real rather than starting at a
        #: parenthesis.
        self._root_name = "(this folder)"
        #: Suppresses recounting while the tree is being rebuilt.
        self._quiet = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self._heading = QLabel("Nothing has changed yet.")
        self._heading.setObjectName("panelhead")
        head.addWidget(self._heading)
        head.addStretch(1)

        self._all_btn = QPushButton("Select all")
        self._all_btn.clicked.connect(lambda: self._set_all(True))
        head.addWidget(self._all_btn)

        self._none_btn = QPushButton("Select none")
        self._none_btn.clicked.connect(lambda: self._set_all(False))
        head.addWidget(self._none_btn)

        self._clear_btn = QPushButton("Clear list")
        self._clear_btn.setToolTip(
            "Forget these changes. The files are untouched; the watcher goes "
            "on watching."
        )
        self._clear_btn.clicked.connect(self.clear)
        head.addWidget(self._clear_btn)

        # The one loud control on this panel, and the only one that acts on a
        # server. It stays disabled - and says so - until something is ticked.
        self._upload_btn = QPushButton("Upload selected")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.setEnabled(False)
        self._upload_btn.clicked.connect(self._on_upload)
        head.addWidget(self._upload_btn)
        layout.addLayout(head)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_menu)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        # Enough rows to be worth opening. Below this the splitter shows a
        # sliver that has to be dragged before it can be read, which is the
        # same as not opening it.
        self._tree.setMinimumHeight(150)
        layout.addWidget(self._tree, 1)

        self._hint = QLabel()
        self._hint.setObjectName("hint")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._recount()

    # ----- taking changes in ------------------------------------------------
    def set_root(self, path: str) -> None:
        """Name the top row after the folder now being watched."""
        self._root_name = os.path.basename(path.rstrip("/" + os.sep)) or path
        group = self._groups.get("")
        if group is not None:
            group.setText(0, self._root_name)
        self._tree.setToolTip(f"Changes seen under {path}")

    def add_changes(self, changes: list[Change]) -> None:
        """Merge a batch from the watcher into the list.

        A file saved repeatedly keeps one row, updated in place: the list is
        "what is different from the server", not a keystroke log. The row does
        move to the state of the newest sighting, so a file that was added and
        then deleted ends up reading as deleted rather than as both.
        """
        if not changes:
            return
        self._quiet = True
        try:
            for change in changes:
                self._absorb(change)
            self._trim()
        finally:
            self._quiet = False
        self._recount()

    def _absorb(self, change: Change) -> None:
        """Put one change on the row it belongs on, moving it if it changed side.

        Deletions live in their own section rather than among the folders,
        and that is not only a matter of reading. A row that cannot be ticked
        sitting inside an auto-tristate folder makes Qt compute that folder's
        state from a child that has no state: a directory holding one edited
        file and one deleted file reported itself unchecked while its file was
        ticked and queued, and the error climbed to every folder above it.
        Keeping each tri-state group's children uniformly checkable is what
        makes the folder ticks mean what they say.
        """
        removed = change.kind is ChangeKind.REMOVED
        wanted = (
            self._deleted_group()
            if removed
            else self._group_for(os.path.dirname(change.rel))
        )
        row = self._rows.get(change.rel)
        if row is not None and row.parent() is not wanted:
            # Added and then deleted, or deleted and then written again.
            self._detach(row)
            row = None
        if row is None:
            row = QTreeWidgetItem(wanted)
            self._rows[change.rel] = row
            wanted.setExpanded(True)
        self._paint(row, change)

    def _deleted_group(self) -> QTreeWidgetItem:
        """The section holding files that have gone from this machine."""
        group = self._groups.get(_DELETED_KEY)
        if group is None:
            group = QTreeWidgetItem(self._tree)
            group.setText(0, "Deleted locally")
            group.setToolTip(
                0,
                "Gone from this machine. Removing them from the server is a "
                "folder sync rule's job, not a watch's - a rule has a defined "
                "scope, and a watch follows whatever the local pane shows.",
            )
            group.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            group.setData(0, _GROUP_ROLE, True)
            group.setData(0, _DIR_ROLE, _DELETED_KEY)
            group.setExpanded(True)
            self._groups[_DELETED_KEY] = group
        # Always last: it is the part you are not being asked to act on.
        index = self._tree.indexOfTopLevelItem(group)
        last = self._tree.topLevelItemCount() - 1
        if 0 <= index < last:
            self._tree.takeTopLevelItem(index)
            self._tree.addTopLevelItem(group)
            group.setExpanded(True)
        return group

    def _paint(self, row: QTreeWidgetItem, change: Change) -> None:
        removed = change.kind is ChangeKind.REMOVED
        # Under a folder the basename is enough; in the deletions section
        # there is no folder above it to supply the rest.
        row.setText(0, change.rel if removed else os.path.basename(change.rel))
        row.setText(1, _KIND_TEXT.get(change.kind, change.kind.value))
        row.setText(2, "" if removed else human_size(change.size))
        row.setText(3, _when(change.modified))
        row.setToolTip(0, change.path)
        row.setData(0, _CHANGE_ROLE, change)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if removed:
            row.setFlags(flags)
            row.setData(0, Qt.ItemDataRole.CheckStateRole, None)
        else:
            row.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable)
            if row.data(0, Qt.ItemDataRole.CheckStateRole) is None:
                row.setCheckState(0, Qt.CheckState.Checked)

    def _group_for(self, rel_dir: str) -> QTreeWidgetItem:
        """The folder row for a relative directory, created if new.

        Folders nest, rather than being one flat row per distinct directory.
        A flat list looks tidier and quietly breaks the thing the panel is
        for: with ``app`` and ``app/Http`` as siblings, ticking ``app`` sends
        the two files directly inside it and silently leaves the subtree
        behind - which is not what anybody means by "the whole folder". Qt
        carries auto-tristate down as many levels as there are, so nesting
        costs a recursive call here and nothing at all in the interaction.
        """
        rel_dir = rel_dir.replace(os.sep, "/").strip("/")
        existing = self._groups.get(rel_dir)
        if existing is not None:
            return existing
        if rel_dir:
            parent_dir, _, name = rel_dir.rpartition("/")
            group = QTreeWidgetItem(self._group_for(parent_dir))
            group.setText(0, name)
        else:
            group = QTreeWidgetItem(self._tree)
            group.setText(0, self._root_name)
        # Qt keeps a tri-state parent in step with its children in both
        # directions, which is the whole of "tick the folder, get the files".
        group.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsAutoTristate
        )
        group.setCheckState(0, Qt.CheckState.Checked)
        group.setData(0, _GROUP_ROLE, True)
        group.setData(0, _DIR_ROLE, rel_dir)
        group.setExpanded(True)
        self._groups[rel_dir] = group
        return group

    def _trim(self) -> None:
        """Drop the oldest rows once the list stops being readable."""
        while len(self._rows) > MAX_ROWS:
            rel, row = next(iter(self._rows.items()))
            self._rows.pop(rel, None)
            self._detach(row)
        self._drop_empty_groups()

    def _drop_empty_groups(self) -> None:
        """Remove folder rows with nothing left in them, deepest first.

        Emptying a child can empty its parent, so this repeats until a pass
        finds nothing - otherwise clearing one file out of ``a/b/c`` leaves
        two folder rows standing over an empty space.
        """
        while True:
            spent = [
                (rel_dir, group)
                for rel_dir, group in self._groups.items()
                if group.childCount() == 0
            ]
            if not spent:
                return
            for rel_dir, group in spent:
                self._groups.pop(rel_dir, None)
                self._detach(group)

    def _detach(self, item: QTreeWidgetItem) -> None:
        """Take one row out of the tree, wherever it happens to sit."""
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
            return
        index = self._tree.indexOfTopLevelItem(item)
        if index >= 0:
            self._tree.takeTopLevelItem(index)

    # ----- selection --------------------------------------------------------
    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._quiet = True
        try:
            for key, group in self._groups.items():
                if key != _DELETED_KEY:
                    group.setCheckState(0, state)
        finally:
            self._quiet = False
        self._recount()

    def selected(self) -> list[Change]:
        """The ticked changes, in the order they are shown."""
        found: list[Change] = []

        def walk(item: QTreeWidgetItem) -> None:
            for index in range(item.childCount()):
                child = item.child(index)
                if child.data(0, _GROUP_ROLE):
                    walk(child)
                    continue
                change = child.data(0, _CHANGE_ROLE)
                if isinstance(change, Change) and (
                    child.checkState(0) == Qt.CheckState.Checked
                ):
                    found.append(change)

        walk(self._tree.invisibleRootItem())
        return found

    def _on_item_changed(self, _item, _column) -> None:
        if not self._quiet:
            self._recount()

    def count(self) -> int:
        """How many changes are listed, deletions included."""
        return len(self._rows)

    def _recount(self) -> None:
        """Say what is here and what pressing the button would do."""
        total = len(self._rows)
        deleted = self._groups.get(_DELETED_KEY)
        removed = deleted.childCount() if deleted is not None else 0
        chosen = self.selected()
        bytes_chosen = sum(change.size for change in chosen)

        changed = total - removed
        parts = []
        if changed:
            parts.append(f"{changed} changed file(s)")
        if removed:
            parts.append(f"{removed} deleted locally")
        self._heading.setText(
            " · ".join(parts) or "Nothing has changed yet."
        )

        self._upload_btn.setEnabled(bool(chosen))
        self._upload_btn.setText(
            f"Upload {len(chosen)} file(s)" if chosen else "Upload selected"
        )
        self._upload_btn.setToolTip(
            f"Send {human_size(bytes_chosen)} to the folder the right-hand "
            "pane is showing"
            if chosen
            else "Tick a file or a folder first"
        )
        for button in (self._all_btn, self._none_btn, self._clear_btn):
            button.setEnabled(bool(total))
        self._hint.setText(
            "Deleted files are listed but not sent: removing them on the "
            "server is a folder sync rule's job."
            if removed
            else ""
        )
        self.count_changed.emit(total)
        self.selection_changed.emit(len(chosen))

    # ----- acting -----------------------------------------------------------
    def _on_upload(self) -> None:
        chosen = self.selected()
        if chosen:
            self.upload_requested.emit(chosen)

    def take_uploaded(self, changes: list[Change]) -> None:
        """Drop the rows that have just been handed to the queue.

        They leave the list on submission rather than on success: what happens
        to them next is the transfer queue's subject, and showing the same file
        as both "changed" and "uploading" in two panels invites the reader to
        wonder which one is true.
        """
        self._quiet = True
        try:
            for change in changes:
                row = self._rows.pop(change.rel, None)
                if row is not None:
                    self._detach(row)
            self._drop_empty_groups()
        finally:
            self._quiet = False
        self._recount()

    def clear(self) -> None:
        self._quiet = True
        try:
            self._tree.clear()
            self._rows.clear()
            self._groups.clear()
        finally:
            self._quiet = False
        self._recount()

    # ----- the small conveniences -------------------------------------------
    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        change = item.data(0, _CHANGE_ROLE)
        if isinstance(change, Change):
            self.reveal_requested.emit(change.path)

    def _on_menu(self, point) -> None:
        item = self._tree.itemAt(point)
        if item is None:
            return
        change = item.data(0, _CHANGE_ROLE)
        menu = QMenu(self._tree)
        if isinstance(change, Change):
            menu.addAction(
                "Show in the local pane",
                lambda: self.reveal_requested.emit(change.path),
            )
        menu.addAction("Forget these rows", self._forget_selected)
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _forget_selected(self) -> None:
        """Take rows out of the list without sending them anywhere.

        Forgetting a folder forgets everything under it - the only reading of
        the word that matches what ticking one does.
        """
        self._quiet = True
        try:
            for item in self._tree.selectedItems():
                # A row inside a selected folder goes when the folder does.
                # Reaching it again afterwards is a use of a deleted item,
                # which Qt answers with a crash rather than an exception.
                if _inside_selection(item):
                    continue
                self._forget(item)
            self._drop_empty_groups()
        finally:
            self._quiet = False
        self._recount()

    def _forget(self, item: QTreeWidgetItem) -> None:
        """Drop one row and everything beneath it, bookkeeping included."""
        for index in reversed(range(item.childCount())):
            self._forget(item.child(index))
        if item.data(0, _GROUP_ROLE):
            self._groups.pop(item.data(0, _DIR_ROLE), None)
        else:
            change = item.data(0, _CHANGE_ROLE)
            if isinstance(change, Change):
                self._rows.pop(change.rel, None)
        self._detach(item)

    def set_theme(self, dark: bool) -> None:
        self.setStyleSheet(theme.pane_stylesheet(dark))


def _inside_selection(item: QTreeWidgetItem) -> bool:
    """Whether some ancestor of this row is selected too."""
    parent = item.parent()
    while parent is not None:
        if parent.isSelected():
            return True
        parent = parent.parent()
    return False


def _when(epoch: float | None) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""
