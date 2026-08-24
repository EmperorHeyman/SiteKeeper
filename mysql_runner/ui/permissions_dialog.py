"""Permissions dialog: the octal box, the nine checkboxes, and the presets.

Everyone who has fixed a web host by hand knows the numbers - 755 on folders,
644 on files, 400 on a key - so the presets come first and the checkbox grid is
there for the rest. The two stay in step: ticking a box updates the number and
typing a number reticks the boxes.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from mysql_runner.transfer import permissions as perm

_SCOPES = (
    ("Everything below it", "all"),
    ("Files only", "files"),
    ("Folders only", "dirs"),
)


class PermissionsDialog(QDialog):
    """Pick a mode for one entry, optionally recursively."""

    def __init__(
        self,
        name: str,
        mode: int | None,
        *,
        is_dir: bool = False,
        allow_recursive: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Permissions — {name}")
        self.setModal(True)
        self._is_dir = is_dir
        self._mode = mode if mode is not None else perm.suggest(is_dir=is_dir)
        self._boxes: dict[tuple[str, str], QCheckBox] = {}
        self._updating = False

        layout = QVBoxLayout(self)

        self._presets = QComboBox()
        self._presets.addItem("Choose a preset…", -1)
        for preset in perm.PRESETS:
            suffix = "  ⚠" if preset.risky else ""
            self._presets.addItem(f"{preset.label}{suffix}", preset.mode)
            self._presets.setItemData(
                self._presets.count() - 1,
                preset.note,
                Qt.ItemDataRole.ToolTipRole,
            )
        self._presets.currentIndexChanged.connect(self._apply_preset)
        layout.addWidget(self._presets)

        grid_box = QGroupBox("Who may do what")
        grid = QGridLayout(grid_box)
        grid.addWidget(QLabel("Read"), 0, 1)
        grid.addWidget(QLabel("Write"), 0, 2)
        grid.addWidget(QLabel("Execute" if not is_dir else "Enter"), 0, 3)
        for row, who in enumerate(perm.WHO, start=1):
            grid.addWidget(QLabel(who.capitalize()), row, 0)
            for column, what in enumerate(perm.WHAT, start=1):
                box = QCheckBox()
                box.toggled.connect(
                    lambda checked, w=who, t=what: self._on_box(w, t, checked)
                )
                self._boxes[(who, what)] = box
                grid.addWidget(box, row, column)
        layout.addWidget(grid_box)

        self._octal = QLineEdit()
        self._octal.setMaxLength(4)
        self._octal.setFixedWidth(70)
        self._octal.textEdited.connect(self._on_octal)
        octal_row = QGridLayout()
        octal_row.addWidget(QLabel("Octal:"), 0, 0)
        octal_row.addWidget(self._octal, 0, 1)
        self._symbolic = QLabel("")
        octal_row.addWidget(self._symbolic, 0, 2)
        octal_row.setColumnStretch(2, 1)
        layout.addLayout(octal_row)

        self._recursive = QCheckBox("Apply to everything inside")
        self._recursive.setEnabled(allow_recursive and is_dir)
        self._recursive.toggled.connect(self._sync_scope)
        layout.addWidget(self._recursive)

        self._scope = QComboBox()
        for label, value in _SCOPES:
            self._scope.addItem(label, value)
        self._scope.setEnabled(False)
        layout.addWidget(self._scope)

        self._warning = QLabel("")
        self._warning.setWordWrap(True)
        self._warning.setObjectName("warning")
        layout.addWidget(self._warning)

        if not allow_recursive:
            note = QLabel(
                "This connection has no shell, so permissions can only be set "
                "one entry at a time."
            )
            note.setWordWrap(True)
            note.setObjectName("hint")
            layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh()

    # ----- syncing --------------------------------------------------------
    def _refresh(self) -> None:
        self._updating = True
        for (who, what), box in self._boxes.items():
            box.setChecked(perm.has_bit(self._mode, who, what))
        self._octal.setText(perm.to_octal(self._mode))
        self._symbolic.setText(perm.format_symbolic(self._mode, is_dir=self._is_dir))
        self._warning.setText(
            "World-writable, or with a set-user-id bit: only do this if you "
            "know you need it."
            if perm.is_risky(self._mode)
            else ""
        )
        self._updating = False

    def _on_box(self, who: str, what: str, checked: bool) -> None:
        if self._updating:
            return
        self._mode = perm.with_bit(self._mode, who, what, checked)
        self._refresh()

    def _on_octal(self, text: str) -> None:
        if self._updating:
            return
        try:
            self._mode = perm.parse_octal(text)
        except ValueError:
            return
        self._updating = True
        for (who, what), box in self._boxes.items():
            box.setChecked(perm.has_bit(self._mode, who, what))
        self._symbolic.setText(perm.format_symbolic(self._mode, is_dir=self._is_dir))
        self._updating = False

    def _apply_preset(self) -> None:
        mode = int(self._presets.currentData())
        if mode < 0:
            return
        self._mode = mode
        self._refresh()

    def _sync_scope(self, recursive: bool) -> None:
        self._scope.setEnabled(recursive)

    # ----- results --------------------------------------------------------
    def mode(self) -> int:
        return self._mode

    def recursive(self) -> bool:
        return self._recursive.isChecked()

    def scope(self) -> str:
        return str(self._scope.currentData()) if self._recursive.isChecked() else "all"
