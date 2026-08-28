"""The repository's history, and the way back to any version in it.

Every deploy this app makes sends whatever is on disk *now*. That is right
almost always and exactly wrong in the one moment it matters: the release that
broke the site is live, the fix is "put Friday's version back", and the working
tree is three commits past Friday. Doing that with git alone means a checkout,
a deploy and a checkout back - three chances to leave the wrong thing on the
server or in the tree.

So this window reads the log, and publishing from it extracts the old bytes
into a scratch folder (see ``transfer/githistory.py``) and uploads *those*.
The working tree is never touched, HEAD never moves, and nothing has to be put
back afterwards.

Two views of a commit answer the two different questions:

* **What this commit changed** - the small, surgical list. Republishing one
  file as it was before a bad change.
* **Every file at this commit** - the whole tree as it stood. Rolling a folder
  back to a known-good release.
"""

from __future__ import annotations

import os
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.transfer.githistory import Commit, commit_files, commit_log, tree_files
from mysql_runner.ui import theme

#: Commits fetched per page. "Load more" asks for another page.
_PAGE = 200

#: More files than this and the tree is trimmed; a first commit can hold
#: thirty thousand of them and the list is not the point.
_MAX_FILE_ROWS = 4000

#: git's letters, spelled out. The colour comes from the theme.
_STATUS = {
    "A": ("added", "green"),
    "M": ("changed", "amber"),
    "D": ("deleted", "red"),
    "T": ("type changed", "amber"),
    "C": ("copied", "green"),
    "R": ("renamed", "amber"),
}


class GitHistoryDialog(QDialog):
    """The commit log of one repository, with a way to publish out of it."""

    #: (commit sha, [repository-relative paths]) - send these, as they were.
    publish_requested = pyqtSignal(str, object)

    # git runs on plain background threads - a log on a network drive takes a
    # noticeable moment - and their answers come back through these. A signal
    # rather than a posted timer, because timers cannot be started from a
    # thread Qt did not create.
    _log_ready = pyqtSignal(object, bool)
    _files_ready = pyqtSignal(str, object)

    def __init__(
        self,
        repo: str,
        *,
        remote: str = "",
        dark: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo = repo
        self._remote = remote
        self._dark = dark
        self._commits: list[Commit] = []
        self._loaded = 0
        self._loading = False
        self._current = ""
        self._files: list[tuple[str, str]] = []  # (status, rel)

        self.setWindowTitle("Git history")
        self.setModal(False)
        self.resize(1040, 620)

        layout = QVBoxLayout(self)

        self._heading = QLabel("")
        self._heading.setObjectName("title")
        self._heading.setWordWrap(True)
        layout.addWidget(self._heading)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_commits())
        splitter.addWidget(self._build_files())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)

        self._note = QLabel("")
        self._note.setObjectName("hint")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        row = QHBoxLayout()
        self._publish = QPushButton("▲ Publish ticked files")
        self._publish.setObjectName("primary")
        self._publish.setToolTip(
            "Upload these files exactly as they were at this commit. Your "
            "working copy is not touched."
        )
        self._publish.clicked.connect(self._on_publish)
        self._publish.setEnabled(False)
        row.addWidget(self._publish)
        self._more = QPushButton("Load older commits")
        self._more.clicked.connect(lambda: self._load(more=True))
        row.addWidget(self._more)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._log_ready.connect(self._on_log)
        self._files_ready.connect(self._on_files)
        self._set_heading()
        self._load()

    # ----- construction ---------------------------------------------------
    def _build_commits(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter commits (message, author, sha)")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._render_commits)
        box.addWidget(self._filter)

        self._log = QTreeWidget()
        self._log.setColumnCount(3)
        self._log.setHeaderLabels(["Commit", "When", "Who"])
        self._log.setRootIsDecorated(False)
        self._log.setAlternatingRowColors(True)
        self._log.setUniformRowHeights(True)
        self._log.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._log.header().resizeSection(1, 120)
        self._log.header().resizeSection(2, 110)
        self._log.currentItemChanged.connect(self._on_commit_chosen)
        box.addWidget(self._log, 1)
        return panel

    def _build_files(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)

        modes = QHBoxLayout()
        self._changed_only = QRadioButton("What this commit changed")
        self._changed_only.setChecked(True)
        self._changed_only.toggled.connect(lambda: self._load_files(self._current))
        self._whole_tree = QRadioButton("Every file at this commit")
        self._whole_tree.setToolTip(
            "The tree as it stood then - for putting a whole folder back to a "
            "known-good release"
        )
        modes.addWidget(self._changed_only)
        modes.addWidget(self._whole_tree)
        modes.addStretch(1)
        box.addLayout(modes)

        self._files_tree = QTreeWidget()
        self._files_tree.setColumnCount(2)
        self._files_tree.setHeaderLabels(["File", "In this commit"])
        self._files_tree.setRootIsDecorated(False)
        self._files_tree.setAlternatingRowColors(True)
        self._files_tree.setUniformRowHeights(True)
        self._files_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._files_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._files_tree.header().resizeSection(1, 130)
        self._files_tree.itemChanged.connect(self._on_tick)
        box.addWidget(self._files_tree, 1)

        picks = QHBoxLayout()
        self._skip_deleted = QCheckBox("Hide files the commit deleted")
        self._skip_deleted.setChecked(True)
        self._skip_deleted.setToolTip(
            "A file deleted by this commit has no contents to publish; "
            "delete it on the server by hand if that is what you want."
        )
        self._skip_deleted.toggled.connect(lambda: self._render_files())
        picks.addWidget(self._skip_deleted)
        picks.addStretch(1)
        tick_all = QPushButton("Tick all")
        tick_all.clicked.connect(lambda: self._set_all(True))
        tick_none = QPushButton("Untick all")
        tick_none.clicked.connect(lambda: self._set_all(False))
        picks.addWidget(tick_all)
        picks.addWidget(tick_none)
        box.addLayout(picks)
        return panel

    # ----- the log --------------------------------------------------------
    def _set_heading(self) -> None:
        name = os.path.basename(self._repo.rstrip("\\/")) or self._repo
        target = self._remote or "the folder open on the right"
        self._heading.setText(
            f"{name} — publishing goes to {target}, as the files were at the "
            "commit you pick. Your working copy is never touched."
        )

    def _load(self, *, more: bool = False) -> None:
        if self._loading:
            return
        self._loading = True
        self._more.setEnabled(False)
        skip = self._loaded if more else 0
        repo = self._repo

        def fetch() -> None:
            # Back to the GUI thread: a plain call from here would touch Qt
            # widgets from a worker thread.
            self._log_ready.emit(commit_log(repo, limit=_PAGE, skip=skip), more)

        threading.Thread(target=fetch, name="git-log", daemon=True).start()

    def _on_log(self, found, more: bool) -> None:
        self._loading = False
        try:
            self._more.setEnabled(True)
        except RuntimeError:
            return  # the window went away while git was running
        if found is None:
            self._note.setText(
                "git could not read this repository. It needs a git executable "
                "on PATH - the commit watcher does not, but reading history "
                "does."
            )
            self._more.setEnabled(False)
            return
        if not more:
            self._commits = list(found)
            self._loaded = len(found)
        else:
            self._commits.extend(found)
            self._loaded += len(found)
        self._more.setEnabled(len(found) == _PAGE)
        self._render_commits()
        if not more and self._log.topLevelItemCount():
            self._log.setCurrentItem(self._log.topLevelItem(0))

    def _render_commits(self) -> None:
        needle = self._filter.text().strip().lower()
        self._log.clear()
        colours = theme.palette(self._dark)
        for commit in self._commits:
            haystack = f"{commit.sha} {commit.subject} {commit.author}".lower()
            if needle and needle not in haystack:
                continue
            row = QTreeWidgetItem(self._log)
            label = commit.subject or "(no message)"
            if commit.refs:
                label = f"{label}   [{commit.refs}]"
            row.setText(0, f"{commit.short}  {label}")
            row.setText(1, commit.date)
            row.setText(2, commit.author)
            row.setData(0, Qt.ItemDataRole.UserRole, commit.sha)
            row.setToolTip(0, f"{commit.sha}\n{commit.subject}")
            if commit.refs:
                row.setForeground(0, QColor(colours.text))
        if not self._log.topLevelItemCount():
            self._note.setText(
                "No commit matches that filter."
                if needle
                else "This repository has no commits yet."
            )

    def _on_commit_chosen(self, item, _previous) -> None:
        sha = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        self._current = str(sha or "")
        self._load_files(self._current)

    # ----- the file list --------------------------------------------------
    def _load_files(self, sha: str) -> None:
        self._files = []
        self._files_tree.clear()
        self._publish.setEnabled(False)
        if not sha:
            return
        repo = self._repo
        whole = self._whole_tree.isChecked()
        self._note.setText("Reading the commit…")

        def fetch() -> None:
            if whole:
                names = tree_files(repo, sha)
                found = (
                    None if names is None else [("=", name) for name in names]
                )
            else:
                found = commit_files(repo, sha)
            self._files_ready.emit(sha, found)

        threading.Thread(target=fetch, name="git-show", daemon=True).start()

    def _on_files(self, sha: str, found) -> None:
        if sha != self._current:
            return  # a different commit was picked while git was running
        if found is None:
            self._note.setText("git could not read that commit.")
            return
        self._files = list(found)
        self._render_files()

    def _render_files(self) -> None:
        tree = self._files_tree
        tree.blockSignals(True)
        tree.clear()
        colours = theme.palette(self._dark)
        hide_deleted = self._skip_deleted.isChecked()
        shown = 0
        deleted = 0
        for status, rel in self._files:
            if status == "D":
                deleted += 1
                if hide_deleted:
                    continue
            if shown >= _MAX_FILE_ROWS:
                break
            row = QTreeWidgetItem(tree)
            row.setText(0, rel)
            label, tone = _STATUS.get(status, ("", ""))
            row.setText(1, label)
            if tone:
                row.setForeground(1, QColor(getattr(colours, tone, colours.text)))
            row.setData(0, Qt.ItemDataRole.UserRole, (rel, status))
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # A deleted file has no contents at this commit, so it can be
            # listed but never ticked.
            row.setCheckState(
                0,
                Qt.CheckState.Unchecked
                if status == "D"
                else Qt.CheckState.Checked,
            )
            if status == "D":
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            shown += 1
        tree.blockSignals(False)
        parts = [f"{shown} file(s) listed"]
        if deleted and hide_deleted:
            parts.append(f"{deleted} deleted by this commit, not shown")
        left = max(0, len(self._files) - shown - (deleted if hide_deleted else 0))
        if left:
            parts.append(f"{left} more not listed")
        self._note.setText("  ·  ".join(parts))
        self._on_tick()

    def _rows(self):
        for index in range(self._files_tree.topLevelItemCount()):
            yield self._files_tree.topLevelItem(index)

    def _set_all(self, checked: bool) -> None:
        tree = self._files_tree
        tree.blockSignals(True)
        for row in self._rows():
            data = row.data(0, Qt.ItemDataRole.UserRole) or ("", "")
            if data[1] == "D":
                continue
            row.setCheckState(
                0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        tree.blockSignals(False)
        self._on_tick()

    def _ticked(self) -> list[str]:
        out: list[str] = []
        for row in self._rows():
            if row.checkState(0) != Qt.CheckState.Checked:
                continue
            data = row.data(0, Qt.ItemDataRole.UserRole)
            if data and data[1] != "D":
                out.append(data[0])
        return out

    def _on_tick(self, *_args) -> None:
        count = len(self._ticked())
        self._publish.setEnabled(bool(count) and bool(self._current))
        self._publish.setText(
            f"▲ Publish {count} file(s)" if count else "▲ Publish ticked files"
        )

    # ----- acting ---------------------------------------------------------
    def _on_publish(self) -> None:
        chosen = self._ticked()
        if chosen and self._current:
            self.publish_requested.emit(self._current, chosen)
