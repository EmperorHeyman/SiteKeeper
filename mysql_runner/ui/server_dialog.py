"""Add/edit dialog for a connection profile.

One dialog covers every connection kind. Picking the kind at the top swaps
which sections are shown - a phpMyAdmin entry needs a URL and an auth mode, a
native MySQL entry needs host/port/database, and a transfer entry needs the
starting directories on both sides.

**Test connection** answers the question this form otherwise leaves open. Six
fields copied out of a hosting email are six chances to be wrong, and until
there was a button here the first attempt at any of them happened later, on a
tab that failed with one line and no way to tell which field was at fault. The
test dials exactly what is typed - not what is saved - so it can be run before
anything is written to the vault, and it runs on a thread of its own so a
server that will never answer does not freeze the dialog. What it concludes is
in connectiontest.py.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
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
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mysql_runner import connectiontest
from mysql_runner.db import mssql_driver
from mysql_runner.storage.models import (
    DEFAULT_PORTS,
    AuthType,
    ConnectionKind,
    Environment,
    ServerProfile,
)
from mysql_runner.transfer import backends, hostkeys
from mysql_runner.ui.testrunner import TestRunner

_KIND_LABELS = {
    ConnectionKind.PHPMYADMIN: "phpMyAdmin (browser tab)",
    ConnectionKind.MYSQL: "MySQL (SQL console)",
    ConnectionKind.MSSQL: "Microsoft SQL Server (SQL console)",
    ConnectionKind.SFTP: "SFTP (file transfer)",
    ConnectionKind.FTP: "FTP (file transfer)",
    ConnectionKind.FTPS: "FTPS (file transfer over TLS)",
}

#: What the ODBC driver box offers when nothing has been chosen.
_AUTO_DRIVER = "Newest installed driver"

#: What the pill beside the Test button says in each state. The words carry
#: the meaning and the colour repeats it, rather than the other way round -
#: a red dot nobody can read is not a result.
_STATE_WORDS = {
    connectiontest.OK: "Works",
    connectiontest.WARN: "Almost",
    connectiontest.FAIL: "Failed",
    "busy": "Testing…",
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

    def __init__(
        self,
        parent=None,
        profile: ServerProfile | None = None,
        *,
        profiles: list[ServerProfile] | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._profiles = profiles or []
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
        # FTP has no shell, so a terminal on an FTP connection is an SSH
        # login to the same host. This is where that port lives once it has
        # been settled - the first terminal asks, and writes the answer here.
        self._ssh_port = QSpinBox()
        self._ssh_port.setRange(0, 65535)
        self._ssh_port.setSpecialValueText("ask the first time")
        self._ssh_port.setToolTip(
            "Where this server's SSH is, for the terminal and for commands "
            "Claude runs. Almost always 22."
        )
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
        self._host_form.addRow("SSH port for the terminal:", self._ssh_port)
        self._host_form.addRow("", self._passive)
        layout.addWidget(self._host_box)

        # ----- SQL Server ---------------------------------------------------
        # Four settings, because a SQL Server that SSMS opens without being
        # asked anything is usually a named instance, reached with the
        # Windows account you are already logged in as, presenting a
        # certificate it signed itself. Any of those missing is a connection
        # that fails with a message about none of them.
        self._mssql_box = QGroupBox("SQL Server")
        mssql_form = QFormLayout(self._mssql_box)
        self._mssql_instance = QLineEdit()
        self._mssql_instance.setPlaceholderText("e.g. SQLEXPRESS (optional)")
        self._mssql_instance.setToolTip(
            "A named instance, the part after the backslash in SSMS's "
            "MACHINE\\SQLEXPRESS. Leave empty for a default instance."
        )
        self._mssql_windows_auth = QCheckBox(
            "Windows Authentication (log in as me)"
        )
        self._mssql_windows_auth.setToolTip(
            "What SSMS calls Windows Authentication: the server trusts the "
            "account this app is running as, and no password is stored."
        )
        self._mssql_windows_auth.toggled.connect(self._on_windows_auth_toggled)
        self._mssql_encrypt = QCheckBox("Encrypt the connection")
        self._mssql_encrypt.setChecked(True)
        self._mssql_trust_cert = QCheckBox(
            "Trust the server's certificate without checking who signed it"
        )
        self._mssql_trust_cert.setChecked(True)
        self._mssql_trust_cert.setToolTip(
            "Leave this on for a server with a self-signed certificate, "
            "which is most in-house SQL Servers. Turn it off only when the "
            "certificate was issued by an authority this PC trusts."
        )
        self._mssql_driver = QComboBox()
        self._mssql_driver.setToolTip(
            "Which Microsoft ODBC driver to dial with. The newest installed "
            "one is right unless a server is too old for it."
        )
        mssql_form.addRow("Instance:", self._mssql_instance)
        mssql_form.addRow("", self._mssql_windows_auth)
        mssql_form.addRow("", self._mssql_encrypt)
        mssql_form.addRow("", self._mssql_trust_cert)
        mssql_form.addRow("ODBC driver:", self._mssql_driver)
        self._mssql_hint = QLabel("")
        self._mssql_hint.setObjectName("hint")
        self._mssql_hint.setWordWrap(True)
        mssql_form.addRow(self._mssql_hint)
        layout.addWidget(self._mssql_box)

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

        # ----- SSH ---------------------------------------------------------
        self._ssh_box = QGroupBox("SSH")
        ssh_form = QFormLayout(self._ssh_box)
        self._use_agent = QCheckBox("Use the keys in my SSH agent")
        self._use_agent.setChecked(True)
        self._use_agent.setToolTip(
            "Pageant, the ssh-agent built into Windows, or 1Password. This is "
            "how most people who never type a password connect at all."
        )
        self._use_default_keys = QCheckBox("Also try the keys in ~/.ssh")
        self._use_default_keys.setToolTip(
            "Off by default: a server that allows only three attempts can "
            "refuse the key that would have worked, after three that were "
            "never meant for it."
        )
        ssh_form.addRow(self._use_agent)
        ssh_form.addRow(self._use_default_keys)

        self._jump = QComboBox()
        self._jump.setToolTip(
            "Reach this server through another saved connection. Its "
            "credentials are the ones already in the vault."
        )
        ssh_form.addRow("Connect via:", self._jump)
        self._proxy_command = QLineEdit()
        self._proxy_command.setPlaceholderText(
            "Optional, e.g. ssh -W %h:%p bastion (advanced)"
        )
        self._proxy_command.setToolTip(
            "An OpenSSH-style ProxyCommand. %h, %p and %r become the host, "
            "port and username. Used instead of the jump host above."
        )
        ssh_form.addRow("Proxy command:", self._proxy_command)

        self._forget_key = QPushButton("Forget this server's host key")
        self._forget_key.setToolTip(
            "Use this only when the server was genuinely rebuilt or rekeyed. "
            "Sitekeeper will ask you to confirm its identity again next time."
        )
        self._forget_key.clicked.connect(self._on_forget_key)
        ssh_form.addRow("", self._forget_key)
        ssh_hint = QLabel(
            "The jump host is reached directly - one hop, even if it names a "
            "jump host of its own."
        )
        ssh_hint.setWordWrap(True)
        ssh_hint.setObjectName("hint")
        ssh_form.addRow(ssh_hint)
        layout.addWidget(self._ssh_box)

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

        # ----- test ---------------------------------------------------------
        # On the same row as Save, on the other side of it: this is the thing
        # you do before saving, and it is not the loud action here.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        self._test_btn = QPushButton("Test connection")
        self._test_btn.setToolTip(
            "Connect with what is typed here and report what answered. "
            "Nothing is saved and nothing on the server is changed."
        )
        self._test_btn.clicked.connect(self._on_test)
        self._test_pill = QLabel("Not tested")
        self._test_pill.setObjectName("pill")
        self._test_pill.setProperty("state", "")
        self._test_detail = QLabel("")
        self._test_detail.setObjectName("hint")
        self._test_detail.setWordWrap(True)
        self._test_detail.setVisible(False)
        self._test_detail.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        self._test_detail.setTextInteractionFlags(
            self._test_detail.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self._test_btn)
        test_row.addWidget(self._test_pill)
        test_row.addStretch(1)
        test_row.addWidget(buttons)
        layout.addWidget(self._test_detail)
        layout.addLayout(test_row)

        # A result stops being true the moment the thing it was about
        # changes, and "Works" in green beside a password that has been
        # edited since is worse than no answer at all.
        for field in (
            self._url, self._host, self._username, self._password,
            self._database, self._mssql_instance, self._remote_dir,
            self._key_path,
        ):
            field.textEdited.connect(self._forget_test_result)
        self._port.valueChanged.connect(self._forget_test_result)
        self._mssql_windows_auth.toggled.connect(self._forget_test_result)

        self._tester = TestRunner(self)
        self._tester.finished.connect(self._on_test_done)
        #: True while a test started by an accepted host key is re-running, so
        #: that question is only ever asked once per press.
        self._test_retried = False

        if profile:
            self._load(profile)
        self._on_kind_changed()

    # ----- kind-driven layout --------------------------------------------
    def _current_kind(self) -> ConnectionKind:
        return self._kind.currentData()

    def _on_kind_changed(self) -> None:
        # Tested as an SFTP account, then switched to FTP: same host, same
        # password, different protocol, and the answer no longer applies.
        self._forget_test_result()
        kind = self._current_kind()
        is_web = kind == ConnectionKind.PHPMYADMIN
        is_sql = kind.is_sql
        is_mssql = kind == ConnectionKind.MSSQL
        is_ftp = kind in (ConnectionKind.FTP, ConnectionKind.FTPS)
        is_sftp = kind == ConnectionKind.SFTP

        self._web_box.setVisible(is_web)
        self._host_box.setVisible(not is_web)
        self._ssh_box.setVisible(is_sftp)
        self._mssql_box.setVisible(is_mssql)
        if is_sftp:
            self._fill_jump_choices()
            self._sync_forget_button()
        if is_mssql:
            self._fill_driver_choices()
            self._on_windows_auth_toggled(self._mssql_windows_auth.isChecked())
        else:
            # Windows authentication greys these out; leaving that behind
            # when the kind changes would lock the fields of an FTP account.
            self._username.setEnabled(True)
            self._password.setEnabled(True)
        self._dirs_box.setVisible(kind.is_transfer)
        self._sql_box.setVisible(is_web or is_sql)

        _set_row_visible(self._host_form, self._database, is_sql)
        _set_row_visible(self._host_form, self._key_widget, is_sftp)
        _set_row_visible(self._host_form, self._passive, is_ftp)
        # SFTP shells on the port it is already talking to; only the
        # protocols that have to borrow SSH need to be told where it is.
        _set_row_visible(self._host_form, self._ssh_port, is_ftp)

        default_port = DEFAULT_PORTS.get(kind, 0)
        self._port.setToolTip(
            f"Leave at 'default' to use {default_port}" if default_port else ""
        )
        if is_sftp:
            self._password_hint.setText(
                "With a private key set, this is the key's passphrase."
            )
        elif not is_mssql:
            # A SQL Server profile's hint belongs to the Windows-auth box,
            # which has already set it by the time this runs.
            self._password_hint.setText("")
        # The dialog shrinks when sections disappear; let Qt re-fit it.
        self.adjustSize()

    # ----- testing --------------------------------------------------------
    def _on_test(self) -> None:
        """Dial what is on the form right now, on a thread of its own."""
        if self._tester.busy:
            return  # already running; the button is disabled anyway
        complaint = self._incomplete()
        if complaint:
            self._show_test_result(
                connectiontest.TestResult(connectiontest.FAIL, complaint)
            )
            return
        profile = self.result_profile()
        jump, problem = backends.jump_for(profile, self._profile_by_id)
        if problem:
            self._show_test_result(
                connectiontest.TestResult(connectiontest.FAIL, problem)
            )
            return

        self._set_testing(True)
        self._tester.start(profile, jump)

    def _incomplete(self) -> str:
        """Why there is nothing to test yet, or "" when there is."""
        kind = self._current_kind()
        if kind == ConnectionKind.PHPMYADMIN:
            if not self._url.text().strip():
                return "Enter the phpMyAdmin URL first."
            return ""
        if not self._host.text().strip():
            return "Enter the server host name or address first."
        return ""

    def _profile_by_id(self, profile_id: str):
        """The other saved connections, for resolving a named jump host."""
        for candidate in self._profiles:
            if candidate.id == profile_id:
                return candidate
        return None

    def _on_test_done(self, result: object) -> None:
        self._set_testing(False)
        if not isinstance(result, connectiontest.TestResult):
            return
        unknown = result.host_key
        if unknown is not None and not self._test_retried:
            # The only failure with an answer the user can give here. Ask it,
            # and if they vouch for the server, finish the test they asked
            # for rather than making them press the button again.
            from mysql_runner.ui.host_key_dialog import ask

            if ask(unknown, self):
                self._test_retried = True
                self._on_test()
                return
        self._test_retried = False
        self._show_test_result(result)

    def _show_test_result(self, result) -> None:
        self._test_pill.setText(_STATE_WORDS.get(result.state, "Failed"))
        self._set_pill_state(result.state)
        body = "\n".join(line for line in result.facts if line)
        self._test_detail.setText(
            f"{result.summary}\n{body}" if body else result.summary
        )
        self._test_detail.setVisible(True)
        self.adjustSize()

    def _set_testing(self, busy: bool) -> None:
        self._test_btn.setEnabled(not busy)
        if busy:
            self._test_pill.setText(_STATE_WORDS["busy"])
            self._set_pill_state("busy")
            self._test_detail.setVisible(False)

    def _forget_test_result(self, *_args) -> None:
        """Drop a result that no longer describes what is on the form."""
        if self._tester.busy or self._test_pill.text() == "Not tested":
            return
        self._test_pill.setText("Not tested")
        self._set_pill_state("")
        self._test_detail.setVisible(False)

    def _set_pill_state(self, state: str) -> None:
        """Restyle the pill: Qt only re-reads a property selector on demand."""
        self._test_pill.setProperty("state", state)
        self._test_pill.style().unpolish(self._test_pill)
        self._test_pill.style().polish(self._test_pill)

    def done(self, code: int) -> None:  # noqa: D102 - Qt's own signature
        # Covers OK, Cancel, Escape and the window's close button, which is
        # four ways to leave a dialog with a test still running.
        self._tester.stop()
        super().done(code)

    # ----- SQL Server -----------------------------------------------------
    def _fill_driver_choices(self) -> None:
        """Offer the ODBC drivers this machine actually has."""
        current = self._mssql_driver.currentData() or (
            self._profile.mssql_odbc_driver if self._profile else ""
        )
        installed = mssql_driver.installed_drivers()
        self._mssql_driver.blockSignals(True)
        self._mssql_driver.clear()
        self._mssql_driver.addItem(_AUTO_DRIVER, "")
        for name in installed:
            self._mssql_driver.addItem(name, name)
        index = self._mssql_driver.findData(current)
        self._mssql_driver.setCurrentIndex(max(0, index))
        self._mssql_driver.blockSignals(False)
        # Saying this here, while the connection is being written, beats
        # saying it when the tab fails to open half an hour later.
        self._mssql_hint.setText(
            mssql_driver.missing_driver_message()
            or f"Using {mssql_driver.default_driver()}."
        )

    def _on_windows_auth_toggled(self, enabled: bool) -> None:
        """A Windows login has no username or password to store."""
        self._username.setEnabled(not enabled)
        self._password.setEnabled(not enabled)
        if self._current_kind() == ConnectionKind.MSSQL:
            self._password_hint.setText(
                "Signed in as this PC's Windows account; nothing is stored."
                if enabled
                else ""
            )

    # ----- SSH ------------------------------------------------------------
    def _fill_jump_choices(self) -> None:
        """Offer every other SFTP connection as a possible bastion."""
        current = self._jump.currentData() or (
            self._profile.jump_profile_id if self._profile else ""
        )
        self._jump.blockSignals(True)
        self._jump.clear()
        self._jump.addItem("Nothing - connect directly", "")
        mine = self._profile.id if self._profile else ""
        for candidate in self._profiles:
            # Not itself, and not something that cannot forward.
            if candidate.id == mine or candidate.kind != ConnectionKind.SFTP:
                continue
            self._jump.addItem(
                f"{candidate.label} ({candidate.host})", candidate.id
            )
        index = self._jump.findData(current)
        self._jump.setCurrentIndex(max(0, index))
        self._jump.blockSignals(False)

    def _sync_forget_button(self) -> None:
        """Only offer to forget a key when there is one to forget."""
        host = self._host.text().strip()
        port = int(self._port.value()) or DEFAULT_PORTS.get(
            ConnectionKind.SFTP, 22
        )
        known = bool(host) and hostkeys.is_known(host, port)
        self._forget_key.setEnabled(known)
        self._forget_key.setText(
            "Forget this server's host key"
            if known
            else "No host key recorded yet"
        )

    def _on_forget_key(self) -> None:
        host = self._host.text().strip()
        port = int(self._port.value()) or DEFAULT_PORTS.get(
            ConnectionKind.SFTP, 22
        )
        if not host:
            return
        confirm = QMessageBox.question(
            self,
            "Forget host key",
            f"Forget the identity recorded for {host}?\n\n"
            "The next connection will ask you to confirm its fingerprint "
            "again. Only do this if the server was genuinely rebuilt or "
            "rekeyed - if it was not, a mismatch is a warning worth heeding.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        hostkeys.forget(host, port)
        self._sync_forget_button()

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
        self._ssh_port.setValue(profile.ssh_port)
        self._use_agent.setChecked(profile.use_agent)
        self._use_default_keys.setChecked(profile.use_default_keys)
        self._proxy_command.setText(profile.proxy_command)
        self._mssql_instance.setText(profile.mssql_instance)
        self._mssql_windows_auth.setChecked(profile.mssql_windows_auth)
        self._mssql_encrypt.setChecked(profile.mssql_encrypt)
        self._mssql_trust_cert.setChecked(profile.mssql_trust_cert)

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
        if (
            self._current_kind() == ConnectionKind.MSSQL
            and not self._mssql_windows_auth.isChecked()
            and not self._username.text().strip()
        ):
            QMessageBox.warning(
                self,
                "Missing login",
                "Enter the SQL login, or tick Windows Authentication to "
                "connect as the account you are signed in with.",
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
            "ssh_port": int(self._ssh_port.value()),
            "use_agent": self._use_agent.isChecked(),
            "use_default_keys": self._use_default_keys.isChecked(),
            "jump_profile_id": str(self._jump.currentData() or ""),
            "proxy_command": self._proxy_command.text().strip(),
            "mssql_instance": self._mssql_instance.text().strip(),
            "mssql_windows_auth": self._mssql_windows_auth.isChecked(),
            "mssql_encrypt": self._mssql_encrypt.isChecked(),
            "mssql_trust_cert": self._mssql_trust_cert.isChecked(),
            "mssql_odbc_driver": str(self._mssql_driver.currentData() or ""),
        }
        if self._profile:
            # ``order`` is where this connection sits in its group. It is not
            # on this form and never was, so rebuilding the profile without it
            # sent every edited connection back to the top of its heading.
            return ServerProfile(
                id=self._profile.id, order=self._profile.order, **kwargs
            )
        return ServerProfile(**kwargs)
