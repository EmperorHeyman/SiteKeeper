"""The replace history: what was overwritten, and how to put it back.

Every overwrite the app performs is journalled with a copy of the bytes that
were about to be lost (see ``transfer/history.py``), so this is a genuine undo
rather than a list of regrets.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mysql_runner.transfer.history import HistoryStore

_COLUMNS = ("When", "What", "Where", "Size", "Status")


class HistoryDialog(QDialog):
    """Browse the shadow backups and restore one."""

    undo_requested = pyqtSignal(str)  # entry id

    def __init__(
        self,
        store: HistoryStore,
        *,
        profile_id: str = "",
        profile_label: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replace history")
        self.setModal(False)
        self.resize(780, 460)
        self._store = store
        self._profile_id = profile_id
        self._all = False

        layout = QVBoxLayout(self)
        header = QLabel(
            f"Files this app overwrote for {profile_label or 'this connection'}. "
            "Restoring puts the saved copy back where it came from."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(False)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tree.itemDoubleClicked.connect(lambda *_: self._on_undo())
        layout.addWidget(self._tree, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._undo_btn = QPushButton("Restore this version")
        self._undo_btn.clicked.connect(self._on_undo)
        scope_btn = QPushButton("Show every connection")
        scope_btn.setCheckable(True)
        scope_btn.toggled.connect(self._on_scope)
        clear_btn = QPushButton("Forget everything")
        clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self._undo_btn)
        row.addWidget(scope_btn)
        row.addStretch(1)
        row.addWidget(clear_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.refresh()

    # ----- content --------------------------------------------------------
    def refresh(self) -> None:
        self._tree.clear()
        entries = self._store.entries(
            profile_id="" if self._all else self._profile_id
        )
        for entry in entries:
            row = QTreeWidgetItem(self._tree)
            row.setText(0, _when(entry.when))
            row.setText(1, entry.describe())
            row.setText(2, entry.target)
            row.setText(3, _human_size(entry.size))
            row.setText(4, _status_text(entry))
            row.setData(0, Qt.ItemDataRole.UserRole, entry.id)
            if not entry.can_undo:
                row.setDisabled(True)
        total = _human_size(self._store.total_bytes())
        self._status.setText(
            f"{len(entries)} entr(ies), {total} of saved copies in the cache at "
            f"{self._store.root}"
        )

    def _selected_id(self) -> str:
        rows = self._tree.selectedItems()
        if not rows:
            return ""
        return str(rows[0].data(0, Qt.ItemDataRole.UserRole) or "")

    # ----- actions --------------------------------------------------------
    def _on_undo(self) -> None:
        entry_id = self._selected_id()
        if not entry_id:
            self._status.setText("Pick an entry to restore.")
            return
        self.undo_requested.emit(entry_id)

    def _on_scope(self, every: bool) -> None:
        self._all = every
        self.refresh()

    def _on_clear(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Forget the history?",
            "Delete every saved copy? Nothing can be restored afterwards.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        count = self._store.clear()
        self._status.setText(f"Removed {count} saved cop(ies).")
        self.refresh()


def _status_text(entry) -> str:
    if entry.undone:
        return "restored"
    if not entry.backup:
        return entry.note or "no copy kept"
    if not entry.can_undo:
        return "copy is gone"
    return "can be restored"


def _when(stamp: float) -> str:
    if not stamp:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _human_size(size: int) -> str:
    if size <= 0:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
