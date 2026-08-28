"""The transfer queue: what is running, what is waiting, and control over both.

A progress bar tells you something is happening; this tells you *what*. Every
file in the queue is a row you can cancel on its own, push to the front, or
drag into the order you actually want, and the whole queue can be paused
mid-file and resumed later.

Each run of the queue is one timestamped batch, newest at the top - the thing
that just happened is the thing being looked for, and it should never be at the
bottom of a scroll. Batches are grouped by the minute rather than the second,
and everything started by one trigger inside that minute folds into a single
headline: "14:32 — 7 file(s) · git sync" with the outcome alongside. That last
part matters most on a busy afternoon, because "why is it uploading?" is
answered by *what started it*, not by how many files it is. Old batches with
nothing unfinished in them are dropped once enough newer ones exist.
"""

from __future__ import annotations

import time
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.transfer.pool import JobState
from mysql_runner.ui import theme

_COLUMNS = ("File", "Direction", "Size", "Progress", "State")

#: Collapsed batches kept around once every file in them has finished.
_KEEP_BATCHES = 6

#: The real state hides here; the visible State column carries error text.
_STATE_ROLE = Qt.ItemDataRole.UserRole + 1

#: Queue runs starting within this window join the previous batch: one sync
#: that touches several subfolders arrives as several submissions.
_BATCH_JOIN_SECONDS = 2.0

#: What started a batch -> how the headline says so. The key travels with the
#: queue from whichever part of the app submitted it (see TransferWorker).
_ORIGINS = {
    "git": "git sync",
    "save": "save",
    "watch": "watched save",
    "edit": "edit in place",
    "sync": "folder sync",
    "compare": "compare",
    "publish": "published from git",
    "manual": "",
}

_FINISHED = frozenset(
    (
        JobState.DONE.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.SKIPPED.value,
    )
)


def _state_colours(dark: bool) -> dict:
    """Colour per state, from the theme, kept muted so a long queue is calm."""
    c = theme.palette(dark)
    return {
        JobState.RUNNING: c.accent,
        JobState.DONE: c.green,
        JobState.FAILED: c.red,
        JobState.CANCELLED: c.text_faint,
        JobState.SKIPPED: c.text_faint,
    }


class TransferQueuePanel(QWidget):
    """A live view of one tab's transfer queue."""

    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    cancel_all_requested = pyqtSignal()
    clear_finished_requested = pyqtSignal()
    cancel_item_requested = pyqtSignal(str)
    prioritize_item_requested = pyqtSignal(str)
    retry_failed_requested = pyqtSignal()
    retry_item_requested = pyqtSignal(str)
    reorder_requested = pyqtSignal(object)
    workers_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None, *, workers: int = 3) -> None:
        super().__init__(parent)
        self._rows: dict[str, QTreeWidgetItem] = {}
        self._batch: QTreeWidgetItem | None = None
        self._batch_started = 0.0
        self._batch_total = 0
        #: Clock minute and trigger of the batch on top, so the next
        #: submission can tell whether it belongs to the same headline.
        self._batch_minute = ""
        self._batch_origin = ""
        self._paused = False
        self._dark = True
        self._colours = _state_colours(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self._summary = QLabel("Nothing queued")
        self._summary.setObjectName("hint")
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setToolTip("Pause every transfer, mid-file if need be")
        self._pause_btn.clicked.connect(self._toggle_pause)
        cancel_btn = QPushButton("Cancel all")
        cancel_btn.clicked.connect(self.cancel_all_requested.emit)
        self._retry_btn = QPushButton("Retry failed")
        self._retry_btn.setToolTip("Queue every failed transfer again")
        self._retry_btn.setVisible(False)  # appears when something has failed
        self._retry_btn.clicked.connect(self.retry_failed_requested.emit)
        clear_btn = QPushButton("Clear finished")
        clear_btn.clicked.connect(self.clear_finished_requested.emit)

        self._workers = QSpinBox()
        self._workers.setRange(1, 16)
        self._workers.setValue(max(1, workers))
        self._workers.setToolTip(
            "How many files to transfer at once, each on its own connection"
        )
        self._workers.valueChanged.connect(self.workers_changed.emit)

        header.addWidget(self._summary, 1)
        header.addWidget(QLabel("At once:"))
        header.addWidget(self._workers)
        header.addWidget(self._pause_btn)
        header.addWidget(cancel_btn)
        header.addWidget(self._retry_btn)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_menu)
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        tree_header = self._tree.header()
        # Every column can be dragged into a different order and resized by
        # hand; the last one soaks up the leftover width.
        tree_header.setSectionsMovable(True)
        tree_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tree_header.setStretchLastSection(True)
        tree_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._tree.setColumnWidth(0, 260)
        for column, width in ((1, 70), (2, 70), (3, 110)):
            self._tree.setColumnWidth(column, width)
        # No height cap: the panel sits in a splitter, so its size is the
        # user's to drag - down to almost nothing if the queue is in the way.
        self._tree.setMinimumHeight(48)
        layout.addWidget(self._tree, 1)
        self._placeholder: QTreeWidgetItem | None = None
        self._show_placeholder()

    # ----- batches ----------------------------------------------------------
    def start_batch(self, total: int, origin: str = "") -> None:
        """A new queue run: fold the previous batches up, group what is coming.

        ``origin`` names the trigger - see ``_ORIGINS``. Two runs join into one
        headline when they fall in the same clock minute *and* came from the
        same trigger: a commit sync that touches six subfolders arrives as six
        submissions and is one event, while a file dragged in by hand half a
        minute later is not, and must not be filed under the sync.
        """
        if total <= 0:
            return
        now = time.monotonic()
        minute = datetime.now().strftime("%H:%M")
        joins = self._batch is not None and (
            now - self._batch_started < _BATCH_JOIN_SECONDS
            or (minute == self._batch_minute and origin == self._batch_origin)
        )
        if joins:
            # The same action, arriving in pieces: extend rather than split.
            self._batch_total += total
            self._batch.setText(0, self._headline(self._batch_total))
            return
        self._clear_placeholder()
        for group in self._groups():
            group.setExpanded(False)
        self._prune_batches()
        # Newest first: index 0, not the end. What just started is what
        # somebody is looking at, so it must not arrive below an afternoon of
        # finished batches.
        batch = QTreeWidgetItem()
        self._tree.insertTopLevelItem(0, batch)
        self._batch_minute = minute
        self._batch_origin = origin
        batch.setText(0, self._headline(total))
        batch.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        font = batch.font(0)
        font.setBold(True)
        batch.setFont(0, font)
        batch.setForeground(0, QColor(theme.palette(self._dark).text_dim))
        batch.setExpanded(True)
        self._batch = batch
        self._batch_started = now
        self._batch_total = total

    def _headline(self, total: int) -> str:
        """"14:32 — 7 file(s) · git sync"."""
        text = f"{self._batch_minute} — {total} file(s)"
        label = _ORIGINS.get(self._batch_origin, self._batch_origin)
        return f"{text}  ·  {label}" if label else text

    def _groups(self) -> list[QTreeWidgetItem]:
        found = []
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item is not self._placeholder:
                found.append(item)
        return found

    def _prune_batches(self, *, keep: int = _KEEP_BATCHES) -> None:
        """Drop the oldest fully-finished batches beyond ``keep``.

        The tree is newest-first, so the oldest are at the bottom - walking it
        the other way would throw away the batches worth keeping.
        """
        groups = list(reversed(self._groups()))
        excess = len(groups) - keep
        for group in groups:
            if excess <= 0:
                break
            if not self._all_finished(group):
                continue
            for child in _children(group):
                item_id = child.data(0, Qt.ItemDataRole.UserRole)
                self._rows.pop(str(item_id), None)
            index = self._tree.indexOfTopLevelItem(group)
            if index >= 0:
                self._tree.takeTopLevelItem(index)
            if group is self._batch:
                self._batch = None
            excess -= 1

    @staticmethod
    def _all_finished(group: QTreeWidgetItem) -> bool:
        return all(
            child.data(0, _STATE_ROLE) in _FINISHED for child in _children(group)
        )

    def _refresh_batch(self, group: QTreeWidgetItem | None) -> None:
        """Keep a batch's headline honest: n/m done, and whether any failed."""
        if group is None:
            return
        done = failed = 0
        total = group.childCount()
        for child in _children(group):
            state = child.data(0, _STATE_ROLE)
            if state == JobState.FAILED.value:
                failed += 1
            elif state in _FINISHED:
                done += 1
        parts = [f"{done + failed}/{total} done"]
        if failed:
            parts.append(f"{failed} failed")
        group.setText(4, ", ".join(parts))
        palette = theme.palette(self._dark)
        if failed:
            colour = palette.red
        elif done == total:
            colour = palette.green
        else:
            colour = palette.text_dim
        group.setForeground(4, QColor(colour))

    # ----- updates --------------------------------------------------------
    def _show_placeholder(self) -> None:
        """A single greyed row, so an empty queue is not a blank hole."""
        if self._placeholder is not None or self._rows:
            return
        row = QTreeWidgetItem(self._tree)
        row.setText(0, "Transfers appear here while they run")
        row.setFirstColumnSpanned(True)
        row.setFlags(Qt.ItemFlag.NoItemFlags)
        row.setForeground(0, QColor(theme.palette(self._dark).text_faint))
        self._placeholder = row

    def _clear_placeholder(self) -> None:
        if self._placeholder is None:
            return
        index = self._tree.indexOfTopLevelItem(self._placeholder)
        if index >= 0:
            self._tree.takeTopLevelItem(index)
        self._placeholder = None

    def update_item(self, item) -> None:
        """Insert or refresh one queue entry."""
        self._clear_placeholder()
        row = self._rows.get(item.id)
        if row is None:
            if self._batch is None:
                self.start_batch(1)
            row = QTreeWidgetItem(self._batch)
            row.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            row.setData(0, Qt.ItemDataRole.UserRole, item.id)
            self._rows[item.id] = row
        row.setText(0, item.name)
        row.setText(1, "upload" if item.upload else "download")
        row.setText(2, _human_size(item.size))
        row.setText(3, _progress_text(item))
        row.setText(4, item.error or item.note or item.state.value)
        row.setData(0, _STATE_ROLE, item.state.value)
        colour = self._colours.get(item.state)
        if colour:
            row.setForeground(4, QColor(colour))
        row.setToolTip(0, f"{item.source}  →  {item.destination}")
        row.setToolTip(4, item.error or item.note or "")
        self._refresh_batch(row.parent())

    def update_stats(self, stats: dict) -> None:
        counts = stats.get("counts", {})
        queued = counts.get(JobState.QUEUED.value, 0)
        running = counts.get(JobState.RUNNING.value, 0)
        done = counts.get(JobState.DONE.value, 0)
        failed = counts.get(JobState.FAILED.value, 0)
        parts = [f"{running} running", f"{queued} waiting", f"{done} done"]
        if failed:
            parts.append(f"{failed} failed")
        self._retry_btn.setVisible(bool(failed))
        total = stats.get("bytes_total", 0)
        if total:
            parts.append(
                f"{_human_size(stats.get('bytes_done', 0))} of {_human_size(total)}"
            )
        self._summary.setText("  ·  ".join(parts))
        self.set_paused(bool(stats.get("paused")))
        workers = int(stats.get("workers", self._workers.value()) or 1)
        if workers != self._workers.value():
            self._workers.blockSignals(True)
            self._workers.setValue(workers)
            self._workers.blockSignals(False)

    def set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")

    def clear(self) -> None:
        self._tree.clear()
        self._rows.clear()
        self._placeholder = None
        self._batch = None
        self._batch_minute = ""
        self._batch_origin = ""
        self._summary.setText("Nothing queued")
        self._show_placeholder()

    def remove_finished(self) -> None:
        """Drop finished rows locally (the pool has already forgotten them)."""
        for item_id, row in list(self._rows.items()):
            if row.data(0, _STATE_ROLE) not in _FINISHED:
                continue
            parent = row.parent()
            if parent is not None:
                parent.removeChild(row)
            else:
                index = self._tree.indexOfTopLevelItem(row)
                if index >= 0:
                    self._tree.takeTopLevelItem(index)
            del self._rows[item_id]
        for group in self._groups():
            if group.childCount():
                self._refresh_batch(group)
                continue
            index = self._tree.indexOfTopLevelItem(group)
            if index >= 0:
                self._tree.takeTopLevelItem(index)
            if group is self._batch:
                self._batch = None
        self._show_placeholder()

    def set_theme(self, dark: bool) -> None:
        """Follow the application theme for the state colours."""
        self._dark = dark
        self._colours = _state_colours(dark)
        if self._placeholder is not None:
            self._placeholder.setForeground(
                0, QColor(theme.palette(dark).text_faint)
            )

    def set_workers(self, count: int) -> None:
        self._workers.blockSignals(True)
        self._workers.setValue(max(1, min(16, count)))
        self._workers.blockSignals(False)

    # ----- interaction ----------------------------------------------------
    def _toggle_pause(self) -> None:
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    def _selected_ids(self) -> list[str]:
        return [
            str(row.data(0, Qt.ItemDataRole.UserRole))
            for row in self._tree.selectedItems()
            if row.data(0, Qt.ItemDataRole.UserRole)
        ]

    def _show_menu(self, position) -> None:
        chosen = self._selected_ids()
        if not chosen:
            return
        menu = QMenu(self)
        to_top = menu.addAction("Transfer next")
        retry = menu.addAction("Retry")
        cancel = menu.addAction("Cancel")
        action = menu.exec(self._tree.viewport().mapToGlobal(position))
        if action is to_top:
            for item_id in chosen:
                self.prioritize_item_requested.emit(item_id)
        elif action is retry:
            for item_id in chosen:
                self.retry_item_requested.emit(item_id)
        elif action is cancel:
            for item_id in chosen:
                self.cancel_item_requested.emit(item_id)

    def _on_rows_moved(self, *_args) -> None:
        """A drag finished: tell the pool the new order, batch by batch."""
        order: list[str] = []
        for index in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(index)
            top_id = top.data(0, Qt.ItemDataRole.UserRole)
            if top_id:
                order.append(str(top_id))
            for child in _children(top):
                child_id = child.data(0, Qt.ItemDataRole.UserRole)
                if child_id:
                    order.append(str(child_id))
        self.reorder_requested.emit(order)


def _children(group: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    return [group.child(index) for index in range(group.childCount())]


def _progress_text(item) -> str:
    if item.state == JobState.DONE:
        return "100%"
    if item.size <= 0:
        return "—" if item.state == JobState.QUEUED else "…"
    percent = int(item.fraction * 100)
    if item.state == JobState.RUNNING and item.rate > 0:
        return f"{percent}%  ({_human_size(int(item.rate))}/s)"
    return f"{percent}%"


def _human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
