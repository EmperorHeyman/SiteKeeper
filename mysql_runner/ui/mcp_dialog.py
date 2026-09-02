"""Hand these connections to Claude: what it may see, what it may do, live.

This window has been three things. It began as a paragraph of instructions
with one fixed command in it, naming ``python -m mysql_runner.mcp`` - which
only works inside a source checkout, so an installed user was told to run
something they did not have. Then it became a command *builder*: tick the
permissions, get the flags, paste it into a terminal.

The builder was still wrong, and the reason is worth writing down. A command
is a thing you run once and then cannot see. Claude Code stores those flags
per project, so the same server was read-only in one folder and could delete
in another, with nothing anywhere able to tell you which - and changing your
mind meant finding a JSON file belonging to another program and restarting
mid-task. Every refusal Claude reported ended in "restart the server with
--allow-x", which is not an instruction anyone should have to follow.

So this is now a control panel, not a generator. The ticks *are* the
permission: each one writes the app's grants file, which the MCP server
re-reads on every tool call. Turning one on lands on the next thing Claude
tries, in every project at once, with nothing restarted. The command at the
bottom carries no flags any more, because there is nothing left to put in it.

Production is granted one connection at a time, beside that connection, the
same way the app's own production guard is disarmed. A single global switch
would mean arming every live site to deploy to one of them.
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
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.mcp.policy import McpPolicy
from mysql_runner.storage.models import Environment
from mysql_runner.ui import theme

#: Filename of the console build that serves MCP. See SitekeeperMCP.spec for
#: why it is a separate executable rather than a flag on the main one.
MCP_EXE = "sitekeeper-mcp.exe"

#: (policy attribute, label, tooltip) - the order they appear in. The labels
#: are quoted back verbatim when the server refuses something, so that a
#: refusal names the box to tick; keep them in step with mcp/tools.py.
PERMISSIONS = (
    (
        "allow_write",
        "Upload files and create folders",
        "Without this Claude can look but not touch.",
    ),
    (
        "allow_delete",
        "Delete files and folders",
        "Deleting on a server is not undoable from Claude's side.",
    ),
    (
        "allow_sql_write",
        "Run SQL that changes data",
        "Off means SELECT-style statements only.",
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


class MCPDialog(QDialog):
    """Grant and revoke what Claude may do, in the place it takes effect."""

    def __init__(self, profiles, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect Claude")
        self._profiles = list(profiles)
        self._policy = McpPolicy.load()
        #: Suppresses saving while the boxes are being set from the policy.
        self._loading = False
        self._scope_boxes: list[tuple[QCheckBox, object]] = []
        self._prod_boxes: list[tuple[QCheckBox, object]] = []
        self._grant_boxes: list[tuple[QCheckBox, str]] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Claude Code and Claude Desktop can use these connections "
            "themselves - browse and read remote files, push files and "
            "folders, run MySQL queries - against this same vault. Everything "
            "here takes effect at once, in every project, with nothing to "
            "restart."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._permissions_group())
        layout.addWidget(self._servers_group())

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        layout.addWidget(QLabel("Not registered yet? Run this once:"))
        self._command = QPlainTextEdit()
        self._command.setReadOnly(True)
        self._command.setMaximumHeight(60)
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
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.close)
        # No blue button here, deliberately. The action of this window is the
        # ticking; Qt hands autoDefault to whatever it finds first inside a
        # button box, which painted "Copy command" as the thing to press and
        # sent the eye to the least important control on the screen.
        for button in buttons.buttons():
            button.setAutoDefault(False)
            button.setDefault(False)
        layout.addWidget(buttons)

        self._load()
        self.resize(640, max(560, self.sizeHint().height()))

    # ----- what Claude may do ---------------------------------------------
    def _permissions_group(self) -> QWidget:
        group = QGroupBox("What Claude may do")
        box = QVBoxLayout(group)
        hint = QLabel("Everything is read-only until ticked.")
        hint.setObjectName("hint")
        box.addWidget(hint)
        for attribute, label, tip in PERMISSIONS:
            check = QCheckBox(label)
            check.setToolTip(tip)
            check.toggled.connect(self._on_changed)
            box.addWidget(check)
            self._grant_boxes.append((check, attribute))
        return group

    # ----- which connections ----------------------------------------------
    def _servers_group(self) -> QWidget:
        group = QGroupBox("Connections Claude may use")
        outer = QVBoxLayout(group)
        every = QCheckBox("Every connection, including ones added later")
        every.setToolTip(
            "Leaves the scope open, so the server sees whatever is in the "
            "vault at the time."
        )
        every.toggled.connect(self._on_every)
        outer.addWidget(every)
        self._every = every

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setMinimumHeight(170)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(12, 4, 4, 4)
        box.setSpacing(2)
        for profile in self._profiles:
            box.addWidget(self._server_row(profile))
        if not self._profiles:
            empty = QLabel("No connections saved yet.")
            empty.setObjectName("hint")
            box.addWidget(empty)
        box.addStretch(1)
        area.setWidget(inner)
        outer.addWidget(area)
        return group

    def _server_row(self, profile) -> QWidget:
        """One connection: whether Claude sees it, and - if live - acts on it."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)

        scope = QCheckBox(profile.label)
        scope.toggled.connect(self._on_changed)
        line.addWidget(scope)
        self._scope_boxes.append((scope, profile))
        line.addStretch(1)

        if profile.environment is Environment.PROD:
            line.addWidget(
                theme.production_badge(
                    "This connection is live. The permissions above do "
                    "nothing here until it is granted separately."
                )
            )
            allow = QCheckBox("Let Claude act on it")
            allow.setToolTip(
                "Granted per connection on purpose: deploying to one live "
                "site should not arm every other one."
            )
            allow.toggled.connect(self._on_changed)
            line.addWidget(allow)
            self._prod_boxes.append((allow, profile))
        return row

    # ----- policy in, policy out -------------------------------------------
    def _load(self) -> None:
        """Set every box from the stored policy without saving it back."""
        self._loading = True
        try:
            for check, attribute in self._grant_boxes:
                check.setChecked(getattr(self._policy, attribute))
            self._every.setChecked(not self._policy.profiles)
            for check, profile in self._scope_boxes:
                check.setChecked(profile.id in self._policy.profiles)
            for check, profile in self._prod_boxes:
                check.setChecked(profile.id in self._policy.production_profiles)
        finally:
            self._loading = False
        self._apply_every()
        self._refresh()

    def _on_every(self) -> None:
        self._apply_every()
        self._on_changed()

    def _apply_every(self) -> None:
        """Ticking every connection makes the individual ones moot, not gone.

        They are disabled rather than cleared so that turning the blanket
        grant back off restores whatever was chosen before it.
        """
        every = self._every.isChecked()
        for check, _profile in self._scope_boxes:
            check.setEnabled(not every)

    def _on_changed(self) -> None:
        if self._loading:
            return
        for check, attribute in self._grant_boxes:
            setattr(self._policy, attribute, check.isChecked())
        self._policy.profiles = (
            []
            if self._every.isChecked()
            else [p.id for check, p in self._scope_boxes if check.isChecked()]
        )
        self._policy.production_profiles = [
            p.id for check, p in self._prod_boxes if check.isChecked()
        ]
        try:
            self._policy.save()
        except OSError as exc:
            self._note.setText(f"Could not save: {exc}")
            return
        self._refresh()

    # ----- saying what is true now -----------------------------------------
    def _refresh(self) -> None:
        self._command.setPlainText(f"claude mcp add sitekeeper -- {server_command()[0]}")
        self._summary.setText(self._summary_text())
        _program, note = server_command()
        lines = [note] if note else []
        if not self._every.isChecked() and not any(
            check.isChecked() for check, _p in self._scope_boxes
        ):
            lines.append(
                "No connections are ticked, so Claude can reach none of them."
            )
        ungranted = [
            p.label
            for check, p in self._prod_boxes
            if not check.isChecked() and self._in_scope(p)
        ]
        if ungranted and self._policy.any_grant():
            lines.append(
                "%s %s live, so the permissions above do not apply there yet."
                % (", ".join(ungranted), "is" if len(ungranted) == 1 else "are")
            )
        # Worth saying once, here: it is the difference between an upload
        # you can cancel and undo and one you cannot, and it depends on
        # something the reader can see - whether this window is open.
        lines.append(
            "While Sitekeeper is open, files Claude uploads go through the "
            "transfer queue of whichever tab already holds that connection, "
            "so they can be cancelled there and undone with Undo replace. "
            "With it closed, Claude uploads them itself and says so."
        )
        lines.append(
            "Revoke the whole thing any time with: claude mcp remove sitekeeper"
        )
        self._note.setText(
            "\n\n".join(lines)
        )

    def _in_scope(self, profile) -> bool:
        if self._every.isChecked():
            return True
        return any(
            check.isChecked() for check, p in self._scope_boxes if p is profile
        )

    def _summary_text(self) -> str:
        """The one line worth reading if you read nothing else in the window."""
        labels = {attribute: label for attribute, label, _tip in PERMISSIONS}
        granted = [
            labels[attribute]
            for check, attribute in self._grant_boxes
            if check.isChecked()
        ]
        if not granted:
            return "Right now Claude can read these connections and change nothing."
        return "Right now Claude may: " + "; ".join(
            label[0].lower() + label[1:] for label in granted
        ) + "."

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._command.toPlainText())
        self._note.setText("Copied. Paste it into a terminal to register it.")
