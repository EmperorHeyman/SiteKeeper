"""What differs between here and the server, and what to do about it.

The comparison itself is done by digests (see ``transfer/hashing.py``), so
"identical" means identical - not "same size, probably fine". This dialog turns
that verdict into an action: tick the files you want and send them one way or
the other.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
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

from mysql_runner.transfer.hashing import DiffStatus
from mysql_runner.ui import theme

#: Heading per status, in the order they matter; colours come from the theme.
_GROUPS = (
    (DiffStatus.DIFFERENT, "Different"),
    (DiffStatus.LOCAL_ONLY, "Only here"),
    (DiffStatus.REMOTE_ONLY, "Only on the server"),
    (DiffStatus.UNKNOWN, "Cannot tell"),
    (DiffStatus.SAME, "Identical"),
)


class CompareDialog(QDialog):
    """The result of a comparison, with the two obvious next steps."""

    upload_requested = pyqtSignal(object)     # list of relative paths
    download_requested = pyqtSignal(object)   # list of relative paths
    refresh_requested = pyqtSignal(bool)      # with hashes?

    def __init__(self, payload: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare with the server")
        self.setModal(False)
        self.resize(820, 560)
        self._report = payload.get("report")
        self._dark = bool(payload.get("dark"))
        local_dir = payload.get("local_dir", "")
        remote_dir = payload.get("remote_dir", "")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{local_dir}\n{remote_dir}"))

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setObjectName("title")
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["File", "Status"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._tree, 1)

        options = QHBoxLayout()
        self._show_same = QCheckBox("Show identical files")
        self._show_same.toggled.connect(self._populate)
        select_all = QPushButton("Tick everything shown")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Untick everything")
        select_none.clicked.connect(lambda: self._set_all(False))
        options.addWidget(self._show_same)
        options.addStretch(1)
        options.addWidget(select_all)
        options.addWidget(select_none)
        layout.addLayout(options)

        actions = QHBoxLayout()
        # Same language as the main window: sending is what this dialog is
        # for, so it is the one loud control, and receiving is its quieter pair.
        upload = QPushButton("▲ Upload ticked")
        upload.setObjectName("primary")
        upload.setToolTip("Send the ticked files to the server")
        upload.clicked.connect(self._on_upload)
        download = QPushButton("▼ Download ticked")
        download.setObjectName("secondary")
        download.setToolTip("Bring the ticked files down from the server")
        download.clicked.connect(self._on_download)
        rehash = QPushButton("Compare again")
        rehash.clicked.connect(lambda: self.refresh_requested.emit(True))
        actions.addWidget(upload)
        actions.addWidget(download)
        actions.addStretch(1)
        actions.addWidget(rehash)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        actions.addWidget(buttons)
        layout.addLayout(actions)

        self.set_report(payload)

    # ----- content --------------------------------------------------------
    def set_report(self, payload: dict) -> None:
        self._report = payload.get("report")
        self._populate()

    def _populate(self) -> None:
        self._tree.clear()
        report = self._report
        if report is None:
            return
        self._summary.setText(
            f"{report.summary()} — compared by {report.compared_by}."
        )
        colours = theme.diff_colours(self._dark)
        for status, heading in _GROUPS:
            if status == DiffStatus.SAME and not self._show_same.isChecked():
                continue
            paths = report.paths(status)
            if not paths:
                continue
            group = QTreeWidgetItem(self._tree)
            group.setText(0, f"{heading} ({len(paths)})")
            group.setFirstColumnSpanned(True)
            group.setExpanded(status != DiffStatus.SAME)
            group.setForeground(0, QColor(colours.get(status.value, "")))
            for rel in paths:
                row = QTreeWidgetItem(group)
                row.setText(0, rel)
                row.setText(1, heading)
                row.setData(0, Qt.ItemDataRole.UserRole, (rel, status.value))
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                pre_ticked = status in (DiffStatus.DIFFERENT, DiffStatus.LOCAL_ONLY)
                row.setCheckState(
                    0,
                    Qt.CheckState.Checked if pre_ticked else Qt.CheckState.Unchecked,
                )

    def _rows(self):
        for index in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(index)
            for child in range(group.childCount()):
                yield group.child(child)

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in self._rows():
            row.setCheckState(0, state)

    def _ticked(self) -> list[str]:
        out: list[str] = []
        for row in self._rows():
            if row.checkState(0) != Qt.CheckState.Checked:
                continue
            data = row.data(0, Qt.ItemDataRole.UserRole)
            if data:
                out.append(data[0])
        return out

    # ----- actions --------------------------------------------------------
    def _on_upload(self) -> None:
        chosen = [
            rel
            for rel in self._ticked()
            if self._status_of(rel) != DiffStatus.REMOTE_ONLY
        ]
        if chosen:
            self.upload_requested.emit(chosen)

    def _on_download(self) -> None:
        chosen = [
            rel
            for rel in self._ticked()
            if self._status_of(rel) != DiffStatus.LOCAL_ONLY
        ]
        if chosen:
            self.download_requested.emit(chosen)

    def _status_of(self, rel: str) -> DiffStatus | None:
        return self._report.status(rel) if self._report is not None else None
