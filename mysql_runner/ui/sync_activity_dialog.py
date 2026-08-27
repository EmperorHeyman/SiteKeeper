"""What the sync watchers saw, and what became of it.

A commit-triggered sync is invisible by design - it fires in the background
and the only trace is one status-line message that is gone a moment later. So
"did it see my commit?" and "did everything actually go up?" had no answer
better than re-running Compare. This window is that answer: every commit (and
every on-save batch) a watcher notices becomes an entry, under it the files
the scan decided to send, and against each file the live outcome of its
upload - queued, uploading, uploaded, or failed with the reason.

The tab logs into this dialog whether or not it is on screen, so opening it
after the fact still shows the whole session.
"""

from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mysql_runner.transfer.pool import JobState
from mysql_runner.ui import theme

_COLUMNS = ("When", "What", "Outcome")

#: Events beyond this many fall off the top; a session is not a logbook.
_MAX_EVENTS = 60

#: What one queue state means in this window's terms.
_STATE_TEXT = {
    JobState.QUEUED: "queued",
    JobState.RUNNING: "uploading…",
    JobState.DONE: "uploaded",
    JobState.CANCELLED: "cancelled",
    JobState.SKIPPED: "skipped",
}


class SyncActivityDialog(QDialog):
    """A live log of commit- and save-triggered syncs for one connection."""

    def __init__(
        self, profile_label: str, *, dark: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sync activity — {profile_label}")
        self.setModal(False)
        self.resize(760, 460)
        self._dark = dark
        #: normcase(local path) -> the file row its transfer reports into.
        self._pending: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        header = QLabel(
            "Every commit and save the folder watchers noticed, what the "
            "comparison decided to send, and whether each file arrived."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setAlternatingRowColors(True)
        self._tree.header().setSectionsMovable(True)
        self._tree.header().setStretchLastSection(True)
        self._tree.setColumnWidth(0, 80)
        self._tree.setColumnWidth(1, 380)
        layout.addWidget(self._tree, 1)

        row = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear)
        row.addWidget(clear_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

    # ----- recording events -------------------------------------------------
    def log_event(self, title: str, *, detail: str = "") -> QTreeWidgetItem:
        """One thing a watcher noticed. Newest first, older ones fold up."""
        for index in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(index).setExpanded(False)
        self._prune()
        event = QTreeWidgetItem()
        self._tree.insertTopLevelItem(0, event)
        event.setText(0, datetime.now().strftime("%H:%M:%S"))
        event.setText(1, title)
        event.setText(2, detail)
        font = event.font(1)
        font.setBold(True)
        event.setFont(1, font)
        event.setExpanded(True)
        return event

    def set_outcome(self, event: QTreeWidgetItem, text: str, *, kind: str = "") -> None:
        """The event's own verdict: "already in step", "waiting", and so on."""
        try:
            event.setText(2, text)
            colour = self._colour(kind)
            if colour is not None:
                event.setForeground(2, colour)
        except RuntimeError:
            pass  # the tab held on to an event that Clear already deleted

    def add_files(self, event: QTreeWidgetItem, files: list[tuple[str, str]]) -> None:
        """Files the sync decided to upload: (local path, shown name) pairs.

        Their rows start as "queued" and are brought to life by
        :meth:`update_transfer` as the queue reports on them.
        """
        try:
            for local, shown in files:
                row = QTreeWidgetItem(event)
                row.setText(1, shown)
                row.setText(2, "queued")
                row.setToolTip(1, local)
                key = os.path.normcase(local)
                row.setData(0, Qt.ItemDataRole.UserRole, key)
                self._pending[key] = row
            self._refresh_event(event)
        except RuntimeError:
            pass  # the tab held on to an event that Clear already deleted

    def add_notes(
        self, event: QTreeWidgetItem, lines: list[str], *, outcome: str
    ) -> None:
        """Informational children with a fixed outcome (removals, mostly)."""
        try:
            for line in lines:
                row = QTreeWidgetItem(event)
                row.setText(1, line)
                row.setText(2, outcome)
                row.setForeground(2, QColor(theme.palette(self._dark).text_faint))
        except RuntimeError:
            pass  # the tab held on to an event that Clear already deleted

    # ----- live transfer results ---------------------------------------------
    def update_transfer(self, item) -> None:
        """A queue snapshot arrived; if it is one of ours, show its state."""
        row = self._pending.get(os.path.normcase(item.local))
        if row is None:
            return
        if item.state == JobState.FAILED:
            text = f"failed — {item.error}" if item.error else "failed"
        else:
            text = _STATE_TEXT.get(item.state, item.state.value)
            if item.state == JobState.SKIPPED and item.note:
                text = f"skipped — {item.note}"
        row.setText(2, text)
        row.setToolTip(2, item.error or item.note or "")
        colour = self._colour(item.state.value)
        if colour is not None:
            row.setForeground(2, colour)
        if item.state.finished:
            self._pending.pop(os.path.normcase(item.local), None)
            self._refresh_event(row.parent())

    def _refresh_event(self, event: QTreeWidgetItem | None) -> None:
        """Roll the children up into the event's own Outcome column."""
        if event is None:
            return
        done = failed = waiting = 0
        for index in range(event.childCount()):
            outcome = event.child(index).text(2)
            if outcome.startswith("failed"):
                failed += 1
            elif outcome in ("queued", "uploading…"):
                waiting += 1
            elif outcome == "uploaded":
                done += 1
        total = done + failed + waiting
        if not total:
            return
        if waiting:
            text = f"{done}/{total} uploaded…"
            kind = ""
        elif failed:
            text = f"{done}/{total} uploaded, {failed} failed"
            kind = JobState.FAILED.value
        else:
            text = f"all {total} uploaded"
            kind = JobState.DONE.value
        self.set_outcome(event, text, kind=kind)

    # ----- housekeeping --------------------------------------------------------
    def _prune(self) -> None:
        while self._tree.topLevelItemCount() >= _MAX_EVENTS:
            oldest = self._tree.takeTopLevelItem(
                self._tree.topLevelItemCount() - 1
            )
            if oldest is None:
                break
            for index in range(oldest.childCount()):
                key = oldest.child(index).data(0, Qt.ItemDataRole.UserRole)
                if key:
                    self._pending.pop(str(key), None)

    def _on_clear(self) -> None:
        self._tree.clear()
        self._pending.clear()

    def set_theme(self, dark: bool) -> None:
        self._dark = dark

    def _colour(self, kind: str) -> QColor | None:
        c = theme.palette(self._dark)
        chosen = {
            JobState.DONE.value: c.green,
            JobState.FAILED.value: c.red,
            JobState.RUNNING.value: c.accent,
            JobState.CANCELLED.value: c.text_faint,
            JobState.SKIPPED.value: c.text_faint,
            "info": c.text_dim,
        }.get(kind)
        return QColor(chosen) if chosen else None
