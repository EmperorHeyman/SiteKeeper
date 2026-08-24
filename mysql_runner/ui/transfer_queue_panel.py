"""The transfer queue: what is running, what is waiting, and control over both.

A progress bar tells you something is happening; this tells you *what*. Every
file in the queue is a row you can cancel on its own, push to the front, or
drag into the order you actually want, and the whole queue can be paused
mid-file and resumed later.
"""

from __future__ import annotations

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
    reorder_requested = pyqtSignal(object)
    workers_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None, *, workers: int = 3) -> None:
        super().__init__(parent)
        self._rows: dict[str, QTreeWidgetItem] = {}
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
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_menu)
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.setMinimumHeight(84)
        self._tree.setMaximumHeight(190)
        self._tree.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._tree, 1)
        self._placeholder: QTreeWidgetItem | None = None
        self._show_placeholder()

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
            row = QTreeWidgetItem(self._tree)
            row.setData(0, Qt.ItemDataRole.UserRole, item.id)
            self._rows[item.id] = row
        row.setText(0, item.name)
        row.setText(1, "upload" if item.upload else "download")
        row.setText(2, _human_size(item.size))
        row.setText(3, _progress_text(item))
        row.setText(4, item.error or item.note or item.state.value)
        colour = self._colours.get(item.state)
        if colour:
            row.setForeground(4, QColor(colour))
        row.setToolTip(0, f"{item.source}  →  {item.destination}")

    def update_stats(self, stats: dict) -> None:
        counts = stats.get("counts", {})
        queued = counts.get(JobState.QUEUED.value, 0)
        running = counts.get(JobState.RUNNING.value, 0)
        done = counts.get(JobState.DONE.value, 0)
        failed = counts.get(JobState.FAILED.value, 0)
        parts = [f"{running} running", f"{queued} waiting", f"{done} done"]
        if failed:
            parts.append(f"{failed} failed")
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
        self._summary.setText("Nothing queued")
        self._show_placeholder()

    def remove_finished(self) -> None:
        """Drop finished rows locally (the pool has already forgotten them)."""
        for item_id, row in list(self._rows.items()):
            state = row.text(4)
            if state in (
                JobState.DONE.value,
                JobState.CANCELLED.value,
                JobState.SKIPPED.value,
            ):
                index = self._tree.indexOfTopLevelItem(row)
                if index >= 0:
                    self._tree.takeTopLevelItem(index)
                del self._rows[item_id]
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
        ]

    def _show_menu(self, position) -> None:
        chosen = self._selected_ids()
        if not chosen:
            return
        menu = QMenu(self)
        to_top = menu.addAction("Transfer next")
        cancel = menu.addAction("Cancel")
        action = menu.exec(self._tree.viewport().mapToGlobal(position))
        if action is to_top:
            for item_id in chosen:
                self.prioritize_item_requested.emit(item_id)
        elif action is cancel:
            for item_id in chosen:
                self.cancel_item_requested.emit(item_id)

    def _on_rows_moved(self, *_args) -> None:
        """A drag finished: tell the pool the new order."""
        order = [
            str(self._tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole))
            for index in range(self._tree.topLevelItemCount())
        ]
        self.reorder_requested.emit(order)


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
