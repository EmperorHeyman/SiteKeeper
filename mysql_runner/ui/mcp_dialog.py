"""Hand these connections to Claude: pick what it may see and do, get the command.

This used to be a paragraph of instructions with one fixed command in it. Two
things were wrong with that. The command named ``python -m mysql_runner.mcp``,
which only works inside a source checkout - an installed user has no such
thing, and the dialog said so rather than offering anything they could use. And
the ``--profiles`` flag, which is the whole of "let Claude see these three
servers and not the other seventeen", was mentioned in a sentence and left to
be typed by hand, exact labels and all.

So the command is built from what is ticked, against the executable that is
actually going to run, and copied in one press.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage.models import Environment

#: Filename of the console build that serves MCP. See SitekeeperMCP.spec for
#: why it is a separate executable rather than a flag on the main one.
MCP_EXE = "sitekeeper-mcp.exe"

#: (flag, label, tooltip) - the order they appear in.
PERMISSIONS = (
    (
        "--allow-write",
        "Upload files and create folders",
        "Without this Claude can look but not touch.",
    ),
    (
        "--allow-delete",
        "Delete files and folders",
        "Deleting on a server is not undoable from Claude's side.",
    ),
    (
        "--allow-sql-write",
        "Run SQL that changes data",
        "Off means SELECT-style statements only.",
    ),
    (
        "--allow-production",
        "Act on servers marked PRODUCTION",
        "The permissions above do nothing on a PROD server without this.",
    ),
)


def server_command() -> tuple[str, str]:
    """The program Claude should run, and a note about where it came from.

    Frozen, the console executable sits next to the GUI one, because that is
    where the installer puts it. From a source checkout there is no such
    executable, so the interpreter running this window is the honest answer -
    and naming it explicitly beats "python", which on a machine with several
    is a coin toss.
    """
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent / MCP_EXE
        if beside.is_file():
            return f'"{beside}"', ""
        return f'"{beside}"', (
            f"{MCP_EXE} is not next to the application. Reinstall Sitekeeper "
            "to put it back."
        )
    root = Path(__file__).resolve().parents[2]
    return f'"{sys.executable}" -m mysql_runner.mcp', (
        f"Running from source, so this uses the current interpreter and needs "
        f"PYTHONPATH={root} set for it, or the command run from that folder. "
        "An installed build ships a self-contained server and needs neither."
    )


def _quote(label: str) -> str:
    """Labels have spaces and commas in them; --profiles takes one argument."""
    return label.replace('"', '""')


class MCPDialog(QDialog):
    """Choose the servers and the permissions; the command follows."""

    def __init__(self, profiles, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect Claude")
        self._profiles = list(profiles)
        self._boxes: list[tuple[QCheckBox, object]] = []
        self._flags: list[tuple[QCheckBox, str]] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Claude Code and Claude Desktop can use these connections "
            "themselves - browse and read remote files, push files and "
            "folders, run MySQL queries - against this same vault. Tick what "
            "it may reach and what it may do, then register it once."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._servers_group())
        layout.addWidget(self._permissions_group())

        layout.addWidget(QLabel("Run this in a terminal:"))
        self._command = QPlainTextEdit()
        self._command.setReadOnly(True)
        self._command.setMaximumHeight(76)
        self._command.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._command)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setObjectName("hint")
        layout.addWidget(self._note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton(
            "Copy command", QDialogButtonBox.ButtonRole.ActionRole
        )
        copy.setDefault(True)
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._refresh()
        self.resize(620, max(520, self.sizeHint().height()))

    # ----- the two lists --------------------------------------------------
    def _servers_group(self) -> QWidget:
        group = QGroupBox("Servers Claude may use")
        outer = QVBoxLayout(group)
        every = QCheckBox("Every server, including ones added later")
        every.setChecked(True)
        every.setToolTip(
            "Leaves --profiles off the command, so the server sees whatever "
            "is in the vault at the time."
        )
        every.stateChanged.connect(self._on_every)
        outer.addWidget(every)
        self._every = every

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setMaximumHeight(150)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(12, 4, 4, 4)
        for profile in self._profiles:
            suffix = " — PRODUCTION" if profile.environment is Environment.PROD else ""
            check = QCheckBox(f"{profile.label}{suffix}")
            check.setEnabled(False)
            check.stateChanged.connect(self._refresh)
            box.addWidget(check)
            self._boxes.append((check, profile))
        if not self._profiles:
            empty = QLabel("No servers saved yet.")
            empty.setObjectName("hint")
            box.addWidget(empty)
        box.addStretch(1)
        area.setWidget(inner)
        outer.addWidget(area)
        return group

    def _permissions_group(self) -> QWidget:
        group = QGroupBox("What Claude may do")
        box = QVBoxLayout(group)
        hint = QLabel("Everything is read-only until ticked.")
        hint.setObjectName("hint")
        box.addWidget(hint)
        for flag, label, tip in PERMISSIONS:
            check = QCheckBox(label)
            check.setToolTip(tip)
            check.stateChanged.connect(self._refresh)
            box.addWidget(check)
            self._flags.append((check, flag))
        return group

    # ----- keeping the command true --------------------------------------
    def _on_every(self) -> None:
        every = self._every.isChecked()
        for check, _profile in self._boxes:
            check.setEnabled(not every)
        self._refresh()

    def _chosen(self) -> list[str]:
        if self._every.isChecked():
            return []
        return [p.label for check, p in self._boxes if check.isChecked()]

    def _command_text(self) -> str:
        program, _note = server_command()
        parts = ["claude mcp add sitekeeper --", program]
        parts.extend(flag for check, flag in self._flags if check.isChecked())
        chosen = self._chosen()
        if chosen:
            parts.append('--profiles "%s"' % ",".join(_quote(c) for c in chosen))
        return " ".join(parts)

    def _refresh(self) -> None:
        self._command.setPlainText(self._command_text())
        _program, note = server_command()
        lines = [note] if note else []
        if not self._every.isChecked() and not self._chosen():
            lines.append(
                "No servers ticked, so Claude will be able to reach none of "
                "them. Tick some, or choose every server."
            )
        prod = [
            p.label
            for check, p in self._boxes
            if check.isChecked() and p.environment is Environment.PROD
        ]
        granting = any(check.isChecked() for check, _f in self._flags[:3])
        allows_prod = self._flags[3][0].isChecked()
        if prod and granting and not allows_prod:
            lines.append(
                "%s is marked PRODUCTION, so the permissions above will not "
                "apply to it without the last one." % ", ".join(prod)
            )
        self._note.setText(
            "\n\n".join(lines)
            or "Read-only unless a permission is ticked. Revoke it any time "
               "with: claude mcp remove sitekeeper"
        )

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._command_text())
        self._note.setText("Copied. Paste it into a terminal to register it.")
