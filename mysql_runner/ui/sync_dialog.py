"""The synced folders of one connection: what is armed, and how.

A sync rule is invisible once it is set - a folder quietly keeping itself on the
server is the whole point - so there has to be one place that lists every rule,
says what triggers it, and lets one be paused or dropped. That is this window.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mysql_runner.transfer.gitwatch import describe_repo
from mysql_runner.transfer.syncrules import SyncMode, SyncRule

_COLUMNS = (
    "Folder", "Server folder", "Trigger", "Subfolders", "Removals", "Git", "State",
)
_FOLDER, _REMOTE, _TRIGGER, _SCOPE, _REMOVALS, _GIT, _STATE = range(7)


class SyncFoldersDialog(QDialog):
    """Every folder this connection keeps in sync, with the trigger for each."""

    #: Reconcile one rule with the server right now.
    sync_now = pyqtSignal(str)
    #: Change one rule's trigger: (rule id, SyncMode value).
    mode_changed = pyqtSignal(str, str)
    #: Turn mirrored removals on or off for one rule.
    removals_changed = pyqtSignal(str, bool)
    #: Include the folder's subfolders, or just the files in it.
    scope_changed = pyqtSignal(str, bool)
    #: Point one rule at a different folder on the server (rule id). The tab
    #: owns the picker, because only it can ask the worker for a listing.
    remote_requested = pyqtSignal(str)
    #: Point one rule at a different folder on this machine (rule id).
    local_requested = pyqtSignal(str)
    #: Forget one rule entirely.
    removed = pyqtSignal(str)

    def __init__(self, profile_label: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Synced folders")
        self.setModal(False)
        self.resize(880, 420)
        self._loading = False

        layout = QVBoxLayout(self)
        header = QLabel(
            f"Folders kept on {profile_label or 'this connection'}. A folder set "
            "to "
            "<b>on save</b> uploads each file as soon as it settles on disk; "
            "<b>on git commit</b> waits for the repository to record a commit and "
            "then reconciles the whole folder. Untick <b>Subfolders</b> to sync "
            "only the files sitting in the folder itself - which is how a site "
            "root is synced without dragging everything under it along. Rules "
            "are remembered and start again the next time this connection is "
            "opened. Either half of a pair can be changed in place - double-"
            "click a <b>Folder</b> or <b>Server folder</b> cell, or use the "
            "buttons below - so a rule pointing one folder out is corrected "
            "rather than dropped and armed again."
        )
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.header().setSectionResizeMode(_FOLDER, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(_REMOTE, QHeaderView.ResizeMode.Stretch)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._tree, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        sync_btn = QPushButton("Sync now")
        sync_btn.setToolTip("Reconcile the selected folder with the server")
        sync_btn.clicked.connect(self._emit_sync)
        save_btn = QPushButton("On save")
        save_btn.setToolTip("Upload each file as soon as it is saved")
        save_btn.clicked.connect(lambda: self._emit_mode(SyncMode.ON_SAVE))
        commit_btn = QPushButton("On git commit")
        commit_btn.setToolTip("Reconcile the folder whenever a commit lands")
        commit_btn.clicked.connect(lambda: self._emit_mode(SyncMode.ON_COMMIT))
        local_btn = QPushButton("Local folder…")
        local_btn.setToolTip("Point this rule at a different folder on this machine")
        local_btn.clicked.connect(lambda: self._emit_edit(self.local_requested))
        remote_btn = QPushButton("Server folder…")
        remote_btn.setToolTip("Point this rule at a different folder on the server")
        remote_btn.clicked.connect(lambda: self._emit_edit(self.remote_requested))
        pause_btn = QPushButton("Pause")
        pause_btn.setToolTip("Keep the rule but stop acting on it")
        pause_btn.clicked.connect(lambda: self._emit_mode(SyncMode.OFF))
        stop_btn = QPushButton("Stop syncing")
        stop_btn.setToolTip("Forget this folder")
        stop_btn.clicked.connect(self._emit_removed)
        for button in (
            sync_btn, save_btn, commit_btn, pause_btn, local_btn, remote_btn
        ):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(stop_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

    # ----- content --------------------------------------------------------
    def set_rules(self, rules: list[SyncRule], *, active: set[str] | None = None) -> None:
        """Redraw the list. ``active`` is the set of rules with a live watcher."""
        running = active or set()
        chosen = self._selected_id()
        self._loading = True
        self._tree.clear()
        for rule in sorted(rules, key=lambda r: r.local.lower()):
            item = QTreeWidgetItem(self._tree)
            item.setText(_FOLDER, rule.local)
            item.setText(_REMOTE, rule.remote)
            item.setText(_TRIGGER, rule.mode.label)
            item.setText(_GIT, describe_repo(rule.local) or "—")
            item.setText(_STATE, _state_of(rule, rule.id in running))
            item.setData(0, Qt.ItemDataRole.UserRole, rule.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                _REMOVALS,
                Qt.CheckState.Checked if rule.delete_remote else Qt.CheckState.Unchecked,
            )
            item.setToolTip(
                _REMOVALS,
                "Remove the copy on the server when a file goes away locally. "
                "A full sync always asks before removing anything.",
            )
            item.setCheckState(
                _SCOPE,
                Qt.CheckState.Checked if rule.recursive else Qt.CheckState.Unchecked,
            )
            item.setToolTip(
                _SCOPE,
                "On covers everything below the folder. Off covers the files in "
                "the folder itself and nothing else.",
            )
            if not os.path.isdir(rule.local):
                item.setToolTip(_FOLDER, "This folder is not on this machine.")
        self._loading = False
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) == chosen:
                self._tree.setCurrentItem(item)
        count = len(rules)
        self._status.setText(
            "No folder on this connection is synced yet. Right-click a local "
            "folder and choose Sync folder."
            if not count
            else f"{count} synced folder(s), {len(running)} of them armed right now."
        )

    def show_message(self, text: str) -> None:
        self._status.setText(text)

    # ----- selection ------------------------------------------------------
    def _selected_id(self) -> str:
        rows = self._tree.selectedItems()
        if not rows:
            return ""
        return str(rows[0].data(0, Qt.ItemDataRole.UserRole) or "")

    def _emit_sync(self) -> None:
        rule_id = self._selected_id()
        if not rule_id:
            self._status.setText("Pick a folder first.")
            return
        self.sync_now.emit(rule_id)

    def _emit_mode(self, mode: SyncMode) -> None:
        rule_id = self._selected_id()
        if not rule_id:
            self._status.setText("Pick a folder first.")
            return
        self.mode_changed.emit(rule_id, mode.value)

    def _emit_edit(self, signal) -> None:
        rule_id = self._selected_id()
        if not rule_id:
            self._status.setText("Pick a folder first.")
            return
        signal.emit(rule_id)

    def _on_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Double-clicking a path edits it; anywhere else runs the sync."""
        rule_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not rule_id:
            return
        if column == _FOLDER:
            self.local_requested.emit(rule_id)
        elif column == _REMOTE:
            self.remote_requested.emit(rule_id)
        else:
            self.sync_now.emit(rule_id)

    def _emit_removed(self) -> None:
        rule_id = self._selected_id()
        if not rule_id:
            self._status.setText("Pick a folder first.")
            return
        self.removed.emit(rule_id)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading or column not in (_REMOVALS, _SCOPE):
            return
        rule_id = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not rule_id:
            return
        checked = item.checkState(column) == Qt.CheckState.Checked
        if column == _REMOVALS:
            self.removals_changed.emit(rule_id, checked)
        else:
            self.scope_changed.emit(rule_id, checked)


def _state_of(rule: SyncRule, armed: bool) -> str:
    if not os.path.isdir(rule.local):
        return "folder missing"
    if rule.mode is SyncMode.OFF:
        return "paused"
    if rule.mode is SyncMode.ON_COMMIT and not describe_repo(rule.local):
        return "not a git repository"
    return "watching" if armed else "idle"
