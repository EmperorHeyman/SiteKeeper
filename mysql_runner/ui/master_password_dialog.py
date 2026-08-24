"""Dialogs for setting and entering the master password."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)


class UnlockDialog(QDialog):
    """Prompt for the master password to unlock an existing vault."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Unlock Sitekeeper")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your master password to unlock saved servers."))

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Master password:", self._password)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def password(self) -> str:
        return self._password.text()


class CreateMasterPasswordDialog(QDialog):
    """First-run dialog to choose a master password - or to go without one."""

    def __init__(self, parent=None, *, allow_no_password: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set Master Password")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Welcome! Choose a master password.\n"
                "It protects the encryption key for your stored credentials."
            )
        )

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Master password:", self._password)
        form.addRow("Confirm:", self._confirm)
        layout.addLayout(form)

        self._skip: QCheckBox | None = None
        if allow_no_password:
            self._skip = QCheckBox("Don't use a master password")
            self._skip.toggled.connect(self._on_skip_toggled)
            layout.addWidget(self._skip)
            hint = QLabel(
                "Your connections still get encrypted, but the key is sealed to "
                "this Windows account instead of a password. You can change this "
                "later in Settings."
            )
            hint.setWordWrap(True)
            hint.setObjectName("hint")
            layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_skip_toggled(self, skipping: bool) -> None:
        self._password.setEnabled(not skipping)
        self._confirm.setEnabled(not skipping)

    def use_password(self) -> bool:
        """Whether the user wants a master password at all."""
        return self._skip is None or not self._skip.isChecked()

    def _on_accept(self) -> None:
        if not self.use_password():
            self.accept()
            return
        if len(self._password.text()) < 6:
            QMessageBox.warning(
                self, "Weak password", "Use at least 6 characters."
            )
            return
        if self._password.text() != self._confirm.text():
            QMessageBox.warning(
                self, "Mismatch", "The passwords do not match."
            )
            return
        self.accept()

    def password(self) -> str:
        return self._password.text()


class ChangeMasterPasswordDialog(QDialog):
    """Dialog to re-key the vault under a new master password."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change Master Password")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Enter your current master password, then choose a new one.\n"
                "Your stored servers stay intact."
            )
        )

        form = QFormLayout()
        self._current = QLineEdit()
        self._current.setEchoMode(QLineEdit.EchoMode.Password)
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Current password:", self._current)
        form.addRow("New password:", self._password)
        form.addRow("Confirm new:", self._confirm)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if len(self._password.text()) < 6:
            QMessageBox.warning(self, "Weak password", "Use at least 6 characters.")
            return
        if self._password.text() != self._confirm.text():
            QMessageBox.warning(self, "Mismatch", "The new passwords do not match.")
            return
        self.accept()

    def current_password(self) -> str:
        return self._current.text()

    def new_password(self) -> str:
        return self._password.text()
