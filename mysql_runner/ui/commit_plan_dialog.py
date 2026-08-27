"""Exactly what a git commit would send, file by file, before it sends it.

The notice strip has room for a count and two folder names. That is enough to
decide *whether* to push and nowhere near enough to decide *where* it lands -
which is the question a commit offer actually raises, because the destination
is a folder pairing the app inferred rather than one anybody configured. So the
counts are a summary of this: every file, with the full server path it is going
to, the deletions it would carry out, and - just as important - the files in the
commit that are **not** going anywhere, with the reason.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
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

from mysql_runner.ui import theme

#: Longer than this and the tree is trimmed; a reset can "change" 40k files.
_MAX_ROWS = 2000


class CommitPlanDialog(QDialog):
    """The upload list a commit produces, against the folder pair on show."""

    #: Push it. True means "and keep pushing every commit from now on".
    push_requested = pyqtSignal(bool)

    def __init__(self, dark: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What this commit would send")
        self.setModal(False)
        self.resize(900, 560)
        self._dark = dark

        layout = QVBoxLayout(self)

        self._heading = QLabel("")
        self._heading.setObjectName("title")
        self._heading.setWordWrap(True)
        layout.addWidget(self._heading)

        self._route = QLabel("")
        self._route.setWordWrap(True)
        self._route.setTextFormat(Qt.TextFormat.RichText)
        self._route.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._route)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["File in the commit", "Where it goes"])
        self._tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tree.header().resizeSection(0, 340)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(12)
        layout.addWidget(self._tree, 1)

        self._note = QLabel("")
        self._note.setObjectName("hint")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)

        row = QHBoxLayout()
        self._push = QPushButton("Push this commit")
        self._push.setObjectName("primary")
        self._push.clicked.connect(lambda: self._emit_push(False))
        self._arm = QPushButton("Push every commit from now on")
        self._arm.setToolTip(
            "Also remembers this folder pair, so later commits go up on their own"
        )
        self._arm.clicked.connect(lambda: self._emit_push(True))
        row.addWidget(self._push)
        row.addWidget(self._arm)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        row.addWidget(buttons)
        layout.addLayout(row)

    # ----- content --------------------------------------------------------
    def set_plan(self, plan: dict) -> None:
        """Redraw for ``plan`` - see ``FileManagerTab._commit_plan``."""
        uploads = plan.get("uploads") or []
        removals = plan.get("removals") or []
        skipped = plan.get("skipped") or []
        commit = plan.get("short") or plan.get("detail") or "HEAD"
        repo = plan.get("repo_label") or plan.get("repo") or ""
        self._heading.setText(
            f"Commit {commit}" + (f" in {repo}" if repo else "")
            + f" — {plan.get('detail', '')}"
        )
        colours = theme.palette(self._dark)
        self._route.setText(
            "<b>From</b> <code>{local}</code><br>"
            "<b>To</b> <code>{remote}</code><br>"
            "<span style='color:{dim}'>Those are the folders open in the two "
            "panes. Move either pane and this list is worked out again for the "
            "new pair.</span>".format(
                local=_escape(str(plan.get("local", ""))),
                remote=_escape(str(plan.get("remote", ""))),
                dim=colours.text_dim,
            )
        )

        self._tree.clear()
        trimmed = 0
        trimmed += self._group(
            f"Upload ({len(uploads)})",
            [(rel, remote) for _local, rel, remote in uploads],
            colours.green,
        )
        trimmed += self._group(
            f"Delete from the server ({len(removals)})",
            [(rel, remote) for rel, remote in removals],
            colours.red,
        )
        trimmed += self._group(
            f"Not sent ({len(skipped)})", skipped, colours.text_faint, expanded=False
        )
        if not uploads and not removals:
            self._note.setText(
                "Nothing in this commit sits under the folder open on the left, "
                "so pushing it would do nothing. Point the local pane at the "
                "folder you deploy from."
            )
        elif trimmed:
            self._note.setText(
                f"{trimmed} more row(s) are not listed; the push covers all of them."
            )
        else:
            self._note.setText("")
        self._push.setEnabled(bool(uploads or removals))
        self._arm.setEnabled(bool(uploads or removals))

    def _group(
        self, heading: str, rows: list, colour: str, *, expanded: bool = True
    ) -> int:
        """Add one section; returns how many rows had to be left out."""
        if not rows:
            return 0
        group = QTreeWidgetItem(self._tree)
        group.setText(0, heading)
        group.setFirstColumnSpanned(True)
        group.setExpanded(expanded)
        group.setForeground(0, QColor(colour))
        shown = rows[:_MAX_ROWS]
        for left, right in shown:
            row = QTreeWidgetItem(group)
            row.setText(0, str(left))
            row.setText(1, str(right))
            row.setToolTip(1, str(right))
        return len(rows) - len(shown)

    def _emit_push(self, arm: bool) -> None:
        self.push_requested.emit(arm)
        self.close()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
