"""Add / edit dialog for a saved phpMyAdmin server connection.

This module was missing from the shipped 1.0.3 build: ``MainWindow._on_add``
and ``MainWindow._on_edit`` reference a global ``ServerDialog`` that was never
bundled, so clicking *Add* (or *Edit*) raised ``NameError`` and crashed the
app. This restores the dialog. It is wired into ``MainWindow``'s namespace by
``mysql_runner.ui.__init__``.
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QVBoxLayout,
    QLineEdit,
    QComboBox,
    QPlainTextEdit,
    QDialogButtonBox,
    QMessageBox,
)

from mysql_runner.storage.models import ServerProfile, AuthType, Environment


_AUTH_CHOICES = [
    ("Automatic", AuthType.AUTO),
    ("Cookie (login form)", AuthType.COOKIE),
    ("HTTP Basic", AuthType.HTTP_BASIC),
]

_ENV_CHOICES = [
    ("None", Environment.NONE),
    ("Development", Environment.DEV),
    ("Staging", Environment.STAGING),
    ("Production", Environment.PROD),
]


class ServerDialog(QDialog):
    """Modal dialog to create or edit a :class:`ServerProfile`."""

    def __init__(self, parent=None, profile=None):
        super().__init__(parent)
        self._profile = profile
        self.setWindowTitle("Edit Server" if profile is not None else "Add Server")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._label = QLineEdit()
        self._label.setPlaceholderText("Display name, e.g. Production DB")

        self._url = QLineEdit()
        self._url.setPlaceholderText("https://phpmyadmin.example.com/")

        self._username = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)

        self._group = QLineEdit()
        self._group.setPlaceholderText("Optional sidebar group")

        self._auth = QComboBox()
        for text, value in _AUTH_CHOICES:
            self._auth.addItem(text, value)

        self._environment = QComboBox()
        for text, value in _ENV_CHOICES:
            self._environment.addItem(text, value)

        self._startup = QPlainTextEdit()
        self._startup.setPlaceholderText("Optional SQL executed after connecting")
        self._startup.setFixedHeight(80)

        form.addRow("Label", self._label)
        form.addRow("URL", self._url)
        form.addRow("Username", self._username)
        form.addRow("Password", self._password)
        form.addRow("Group", self._group)
        form.addRow("Auth type", self._auth)
        form.addRow("Environment", self._environment)
        form.addRow("Startup SQL", self._startup)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if profile is not None:
            self._load(profile)

    def _load(self, profile):
        self._label.setText(profile.label)
        self._url.setText(profile.url)
        self._username.setText(profile.username)
        self._password.setText(profile.password)
        self._group.setText(profile.group)
        self._select(self._auth, profile.auth_type)
        self._select(self._environment, profile.environment)
        self._startup.setPlainText(profile.startup_script)

    @staticmethod
    def _select(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _on_accept(self):
        if not self._label.text().strip():
            QMessageBox.warning(self, "Server", "Please enter a label.")
            return
        if not self._url.text().strip():
            QMessageBox.warning(self, "Server", "Please enter a URL.")
            return
        self.accept()

    def result_profile(self):
        """Build a :class:`ServerProfile` from the current field values.

        When editing, the original profile's ``id`` is preserved so
        ``ServerStore.update`` can locate and replace the existing entry.
        """
        values = dict(
            label=self._label.text().strip(),
            url=self._url.text().strip(),
            username=self._username.text().strip(),
            password=self._password.text(),
            auth_type=self._auth.currentData(),
            group=self._group.text().strip(),
            environment=self._environment.currentData(),
            startup_script=self._startup.toPlainText(),
        )
        if self._profile is not None:
            values["id"] = self._profile.id
        return ServerProfile(**values)
