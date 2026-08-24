"""Add/edit dialog for a connection profile.

One dialog covers every connection kind. Picking the kind at the top swaps
which sections are shown - a phpMyAdmin entry needs a URL and an auth mode, a
native MySQL entry needs host/port/database, and a transfer entry needs the
starting directories on both sides.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage.models import (
    DEFAULT_PORTS,
    AuthType,
    ConnectionKind,
    Environment,
    ServerProfile,
)

_KIND_LABELS = {
    ConnectionKind.PHPMYADMIN: "phpMyAdmin (browser tab)",
    ConnectionKind.MYSQL: "MySQL (SQL console)",
    ConnectionKind.SFTP: "SFTP (file transfer)",
    ConnectionKind.FTP: "FTP (file transfer)",
    ConnectionKind.FTPS: "FTPS (file transfer over TLS)",
}

_AUTH_LABELS = {
    AuthType.AUTO: "Auto-detect",
    AuthType.COOKIE: "phpMyAdmin login form",
    AuthType.HTTP_BASIC: "HTTP Basic Auth",
}

_ENV_LABELS = {
    Environment.NONE: "None",
    Environment.DEV: "Development",
    Environment.STAGING: "Staging",
    Environment.PROD: "Production",
}


def _set_row_visible(form: QFormLayout, field, visible: bool) -> None:
    """Show/hide a form row including its label, across Qt versions."""
    if hasattr(form, "setRowVisible"):
        form.setRowVisible(field, visible)
        return
    field.setVisible(visible)
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)


class ServerDialog(QDialog):
    """Collects/edits the fields of a :class:`ServerProfile`."""

    def __init__(self, parent=None, profile: ServerProfile | None = None) -> None:
        super().__init__(parent)
        self._profile = profile
        self.setWindowTitle("Edit Connection" if profile else "Add Connection")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        # ----- what kind of connection ------------------------------------
        self._kind = QComboBox()
        for kind, text in _KIND_LABELS.items():
            self._kind.addItem(text, kind)
        self._kind.currentIndexChanged.connect(self._on_kind_changed)

        general = QGroupBox("Connection")
        general_form = QFormLayout(general)
        self._label = QLineEdit()
        self._group = QLineEdit()
        self._group.setPlaceholderText("e.g. Production, Client A (optional)")
        self._environment = QComboBox()
        for env, text in _ENV_LABELS.items():
            self._environment.addItem(text, env)
        general_form.addRow("Type:", self._kind)
        general_form.addRow("Display name:", self._label)
        general_form.addRow("Group:", self._group)
        general_form.addRow("Environment:", self._environment)
        layout.addWidget(general)

        # ----- phpMyAdmin -------------------------------------------------
        self._web_box = QGroupBox("phpMyAdmin")
        web_form = QFormLayout(self._web_box)
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://example.com/phpmyadmin/")
        self._auth = QComboBox()
        for auth_type, text in _AUTH_LABELS.items():
            self._auth.addItem(text, auth_type)
        web_form.addRow("URL:", self._url)
        web_form.addRow("Authentication:", self._auth)
        layout.addWidget(self._web_box)

        # ----- host / port ------------------------------------------------
        self._host_box = QGroupBox("Server")
        self._host_form = QFormLayout(self._host_box)
        self._host = QLineEdit()
        self._host.setPlaceholderText("db.example.com or 10.0.0.5")
        self._port = QSpinBox()
        self._port.setRange(0, 65535)
        self._port.setSpecialValueText("default")
        self._database = QLineEdit()
        self._database.setPlaceholderText("Optional starting database")
        self._passive = QCheckBox("Passive mode (recommended)")
        self._key_path = QLineEdit()
        self._key_path.setPlaceholderText("Optional OpenSSH private key")
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_browse = QPushButton("Browse…")
        key_browse.clicked.connect(self._on_browse_key)
        key_row.addWidget(self._key_path, 1)
        key_row.addWidget(key_browse)
        self._key_widget = QWidget()  # container so the whole row can be hidden
        self._key_widget.setLayout(key_row)
        self._host_form.addRow("Host:", self._host)
        self._host_form.addRow("Port:", self._port)
        self._host_form.addRow("Database:", self._database)
        self._host_form.addRow("Private key:", self._key_widget)
        self._host_form.addRow("", self._passive)
        layout.addWidget(self._host_box)

        # ----- credentials -------------------------------------------------
        creds = QGroupBox("Credentials")
        creds_form = QFormLayout(creds)
        self._username = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_hint = QLabel("")
        self._password_hint.setObjectName("hint")
        self._password_hint.setWordWrap(True)
        creds_form.addRow("Username:", self._username)
        creds_form.addRow("Password:", self._password)
        creds_form.addRow("", self._password_hint)
        layout.addWidget(creds)

        # ----- starting directories ---------------------------------------
        self._dirs_box = QGroupBox("Starting directories")
        dirs_form = QFormLayout(self._dirs_box)
        self._remote_dir = QLineEdit()
        self._remote_dir.setPlaceholderText("Server default (e.g. /var/www)")
        self._local_dir = QLineEdit()
        self._local_dir.setPlaceholderText("Your home folder")
        local_row = QHBoxLayout()
        local_row.setContentsMargins(0, 0, 0, 0)
        local_browse = QPushButton("Browse…")
        local_browse.clicked.connect(self._on_browse_local)
        local_row.addWidget(self._local_dir, 1)
        local_row.addWidget(local_browse)
        local_widget = QWidget()
        local_widget.setLayout(local_row)
        dirs_form.addRow("Remote:", self._remote_dir)
        dirs_form.addRow("Local:", local_widget)
        layout.addWidget(self._dirs_box)

        # ----- startup SQL -------------------------------------------------
        self._sql_box = QGroupBox("Startup SQL")
        sql_layout = QVBoxLayout(self._sql_box)
        self._startup = QPlainTextEdit()
        self._startup.setPlaceholderText(
            "Optional SQL run automatically after login, e.g. SET NAMES utf8;"
        )
        self._startup.setFixedHeight(70)
        sql_layout.addWidget(self._startup)
        layout.addWidget(self._sql_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if profile:
            self._load(profile)
        self._on_kind_changed()

    # ----- kind-driven layout --------------------------------------------
    def _current_kind(self) -> ConnectionKind:
        return self._kind.currentData()

    def _on_kind_changed(self) -> None:
        kind = self._current_kind()
        is_web = kind == ConnectionKind.PHPMYADMIN
        is_mysql = kind == ConnectionKind.MYSQL
        is_ftp = kind in (ConnectionKind.FTP, ConnectionKind.FTPS)
        is_sftp = kind == ConnectionKind.SFTP

        self._web_box.setVisible(is_web)
        self._host_box.setVisible(not is_web)
        self._dirs_box.setVisible(kind.is_transfer)
        self._sql_box.setVisible(is_web or is_mysql)

        _set_row_visible(self._host_form, self._database, is_mysql)
        _set_row_visible(self._host_form, self._key_widget, is_sftp)
        _set_row_visible(self._host_form, self._passive, is_ftp)

        default_port = DEFAULT_PORTS.get(kind, 0)
        self._port.setToolTip(
            f"Leave at 'default' to use {default_port}" if default_port else ""
        )
        self._password_hint.setText(
            "With a private key set, this is the key's passphrase."
            if is_sftp
            else ""
        )
        # The dialog shrinks when sections disappear; let Qt re-fit it.
        self.adjustSize()

    # ----- browsing -------------------------------------------------------
    def _on_browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select private key", "", "All files (*)"
        )
        if path:
            self._key_path.setText(path)

    def _on_browse_local(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select local directory", self._local_dir.text()
        )
        if path:
            self._local_dir.setText(path)

    # ----- load / save ----------------------------------------------------
    def _load(self, profile: ServerProfile) -> None:
        index = self._kind.findData(profile.kind)
        if index >= 0:
            self._kind.setCurrentIndex(index)
        self._label.setText(profile.label)
        self._url.setText(profile.url)
        self._username.setText(profile.username)
        self._password.setText(profile.password)
        auth_index = self._auth.findData(profile.auth_type)
        if auth_index >= 0:
            self._auth.setCurrentIndex(auth_index)
        self._group.setText(profile.group)
        env_index = self._environment.findData(profile.environment)
        if env_index >= 0:
            self._environment.setCurrentIndex(env_index)
        self._startup.setPlainText(profile.startup_script)
        self._host.setText(profile.host)
        self._port.setValue(profile.port)
        self._database.setText(profile.database)
        self._remote_dir.setText(profile.remote_dir)
        self._local_dir.setText(profile.local_dir)
        self._key_path.setText(profile.private_key_path)
        self._passive.setChecked(profile.passive)

    def _on_accept(self) -> None:
        if not self._label.text().strip():
            QMessageBox.warning(self, "Missing name", "Please enter a display name.")
            return
        if self._current_kind() == ConnectionKind.PHPMYADMIN:
            url = self._url.text().strip()
            if not url.startswith(("http://", "https://")):
                QMessageBox.warning(
                    self, "Invalid URL", "URL must start with http:// or https://"
                )
                return
        elif not self._host.text().strip():
            QMessageBox.warning(
                self, "Missing host", "Please enter the server host name or address."
            )
            return
        self.accept()

    def result_profile(self) -> ServerProfile:
        """Return a profile built from the field values."""
        kwargs = {
            "label": self._label.text().strip(),
            "url": self._url.text().strip(),
            "username": self._username.text(),
            "password": self._password.text(),
            "auth_type": self._auth.currentData(),
            "group": self._group.text().strip(),
            "environment": self._environment.currentData(),
            "startup_script": self._startup.toPlainText(),
            "kind": self._current_kind(),
            "host": self._host.text().strip(),
            "port": int(self._port.value()),
            "database": self._database.text().strip(),
            "remote_dir": self._remote_dir.text().strip(),
            "local_dir": self._local_dir.text().strip(),
            "private_key_path": self._key_path.text().strip(),
            "passive": self._passive.isChecked(),
        }
        if self._profile:
            return ServerProfile(id=self._profile.id, **kwargs)
        return ServerProfile(**kwargs)
