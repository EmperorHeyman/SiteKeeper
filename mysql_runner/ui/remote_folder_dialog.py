"""Pick a folder on the server without walking the pane there first.

The remote pane can only be moved one listing at a time, which is fine for
looking around and hopeless for "put this over there": getting from ``/`` to
``/var/www/vhosts/example.com/httpdocs`` is six clicks and six round trips,
and every one of them repaints the pane you were using. This dialog is the
other way round - a tree that keeps its shape, expands the branch you ask for
and leaves the pane where it was until you say "go here".

Folders arrive from :meth:`TransferWorker.request_folders`, on the worker's own
browse connection, so opening this while a comparison is running works.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mysql_runner.transfer.base import RemoteFS
from mysql_runner.ui import theme

#: Marks a branch whose children have been asked for but have not arrived.
_LOADING = "loading"
#: Marks the placeholder row that makes an unread branch expandable at all.
_STUB = "stub"


class RemoteFolderDialog(QDialog):
    """A lazily-filled tree of the server's directories."""

    #: Read this directory (the dialog cannot talk to the worker itself).
    folders_requested = pyqtSignal(str)
    #: The user settled on a folder; take the pane there.
    chosen = pyqtSignal(str)

    def __init__(
        self, start: str, *, label: str = "", dark: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose a server folder")
        self.setModal(False)
        self.resize(460, 520)

        #: Remote path -> its row, so an answer can find what asked for it.
        self._items: dict[str, QTreeWidgetItem] = {}
        #: Directories still to be opened on the way to the starting path.
        self._chain: list[str] = []
        self._dark = dark

        layout = QVBoxLayout(self)
        if label:
            heading = QLabel(label)
            heading.setObjectName("hint")
            heading.setWordWrap(True)
            layout.addWidget(heading)

        self._edit = QLineEdit(start or "/")
        self._edit.setPlaceholderText("/var/www")
        self._edit.setToolTip(
            "The folder the pane will open. Type a path here to go straight "
            "to it, or pick one from the tree."
        )
        self._edit.returnPressed.connect(self._accept_path)
        layout.addWidget(self._edit)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        # Folders only, so the arrow column is the whole decoration needed;
        # the sidebar taught us how much dead space a wide indent wastes.
        self._tree.setIndentation(12)
        self._tree.itemExpanded.connect(self._on_expanded)
        self._tree.currentItemChanged.connect(self._on_current)
        self._tree.itemDoubleClicked.connect(lambda *_: self._accept_path())
        layout.addWidget(self._tree, 1)

        self._status = QLabel("Reading the server…")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        refresh = QPushButton("Re-read this folder")
        refresh.setToolTip("Ask the server again, in case it changed")
        refresh.clicked.connect(self._refresh_current)
        row.addWidget(refresh)
        row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        go = buttons.addButton("Go here", QDialogButtonBox.ButtonRole.AcceptRole)
        go.setObjectName("primary")
        go.clicked.connect(self._accept_path)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        # Only the shape of the tree here. Asking for anything before the
        # caller has connected to folders_requested would throw the first
        # answer away, and the first answer is the root - without it every
        # branch below it is unreachable.
        root = self._add_item(None, "/", "/")
        root.setExpanded(True)
        self._chain = _ancestors(start)

    # ----- building -------------------------------------------------------
    def start(self) -> None:
        """Begin reading, once ``folders_requested`` has somewhere to go."""
        self._request(self._chain.pop(0) if self._chain else "/")

    def _add_item(
        self, parent: QTreeWidgetItem | None, name: str, path: str
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name])
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        item.setIcon(0, theme.entry_icon("folder", self._dark))
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        self._items[path] = item
        self._add_stub(item)
        return item

    @staticmethod
    def _add_stub(item: QTreeWidgetItem) -> None:
        """One placeholder child, so the branch shows an arrow before it is read.

        Without it Qt draws a leaf, and a folder that cannot be expanded looks
        like a folder with nothing in it - which for ``/var`` is a lie.
        """
        stub = QTreeWidgetItem(["…"])
        stub.setData(0, Qt.ItemDataRole.UserRole + 1, _STUB)
        stub.setFlags(Qt.ItemFlag.NoItemFlags)
        item.addChild(stub)

    @staticmethod
    def _is_stub(item: QTreeWidgetItem) -> bool:
        return item.data(0, Qt.ItemDataRole.UserRole + 1) == _STUB

    def _clear_children(self, item: QTreeWidgetItem) -> None:
        for child in item.takeChildren():
            path = child.data(0, Qt.ItemDataRole.UserRole)
            if path:
                self._items.pop(path, None)

    # ----- asking ---------------------------------------------------------
    def _request(self, path: str) -> None:
        item = self._items.get(path)
        if item is not None:
            item.setData(0, Qt.ItemDataRole.UserRole + 1, _LOADING)
        self._status.setText(f"Reading {path} …")
        self.folders_requested.emit(path)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return
        if item.childCount() == 1 and self._is_stub(item.child(0)):
            self._request(path)

    def _refresh_current(self) -> None:
        path = self._selected_path()
        item = self._items.get(path)
        if item is None:
            return
        self._clear_children(item)
        self._add_stub(item)
        self._request(path)

    # ----- answers --------------------------------------------------------
    def add_folders(self, path: str, names: list[str]) -> None:
        """The server named the directories inside ``path``."""
        item = self._items.get(path)
        if item is None:
            return
        item.setData(0, Qt.ItemDataRole.UserRole + 1, None)
        self._clear_children(item)
        for name in names:
            self._add_item(item, name, RemoteFS.join(path, name))
        item.setExpanded(True)
        if not names:
            self._status.setText(f"{path} has no subfolders.")
        else:
            self._status.setText(f"{path} — {len(names)} folder(s).")
        # Keep walking down to where the pane already is, and stop the moment
        # a step is missing rather than asking for children of nothing.
        while self._chain:
            nxt = self._chain.pop(0)
            if nxt in self._items:
                self._request(nxt)
                return
            self._chain.clear()
        target = self._items.get(self._edit.text().strip())
        if target is not None:
            self._tree.setCurrentItem(target)
            self._tree.scrollToItem(target)

    def show_error(self, path: str, message: str) -> None:
        """The server refused a directory - say so without losing the tree."""
        self._chain.clear()
        item = self._items.get(path)
        if item is not None:
            item.setData(0, Qt.ItemDataRole.UserRole + 1, None)
            self._clear_children(item)
        self._status.setText(f"{path}: {message}")

    # ----- choosing -------------------------------------------------------
    def _on_current(self, item: QTreeWidgetItem | None, _previous=None) -> None:
        if item is None or self._is_stub(item):
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self._edit.setText(path)

    def _selected_path(self) -> str:
        return self._edit.text().strip() or "/"

    def _accept_path(self) -> None:
        self.chosen.emit(self._selected_path())
        self.accept()


def _ancestors(path: str) -> list[str]:
    """``/var/www/html`` -> ``["/", "/var", "/var/www", "/var/www/html"]``.

    The walk opens every step, because a tree that jumps straight to the leaf
    shows the destination with no way back up to its siblings.
    """
    clean = (path or "/").strip()
    if not clean.startswith("/"):
        return ["/"]
    out = ["/"]
    current = ""
    for part in clean.strip("/").split("/"):
        if not part:
            continue
        current = f"{current}/{part}"
        out.append(current)
    return out
