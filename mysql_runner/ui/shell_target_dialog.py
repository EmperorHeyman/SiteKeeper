"""The question an FTP connection has to be asked before it gets a terminal.

FTP has no shell, so a terminal on an FTP or FTPS connection is really an SSH
login to the same machine with the same account - which is almost always
there, and is how most of these servers are administered. What it is not is
free: the password saved for one service gets sent to a different one, on a
port nobody has confirmed is even the right server's.

That is a decision, so it is put as one, once, and then remembered on the
profile (``ssh_port``). Everything after this is ordinary SSH - an unknown
host key still stops and asks, in :mod:`mysql_runner.ui.host_key_dialog`.

SFTP connections never see this dialog: their shell is on the port they are
already talking to.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from mysql_runner.storage.models import ServerProfile
from mysql_runner.transfer import shellaccess


class ShellTargetDialog(QDialog):
    """Confirm borrowing SSH for a connection whose protocol has no shell."""

    def __init__(self, profile: ServerProfile, parent=None) -> None:
        super().__init__(parent)
        self._profile = profile
        self.setWindowTitle("Open a terminal")
        self.setModal(True)
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        headline = QLabel(f"{profile.label} is an {profile.kind.value.upper()} connection.")
        headline.setObjectName("title")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        body = QLabel(
            f"{profile.kind.value.upper()} has no shell of its own, so the "
            f"terminal logs in to <b>{profile.host}</b> over SSH as "
            f"<b>{profile.username or 'anonymous'}</b>, with the password "
            "saved here. Nearly every server reached over FTP answers on SSH "
            "with the same account - but this does send that password to a "
            "different service, which is why it asks."
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        form = QFormLayout()
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(shellaccess.DEFAULT_SSH_PORT)
        self._port.setToolTip(
            "Almost always 22. Some hosts move SSH; your control panel says "
            "where."
        )
        form.addRow("SSH port:", self._port)
        layout.addLayout(form)

        hint = QLabel(
            "Remembered for this connection, so it is asked once. Change it "
            "later in Edit → Server → SSH port for the terminal."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # One loud button, and it takes both halves to get there: the
        # objectName paints it whether or not this window has focus (the
        # button box's :default rule only applies while it does), and Cancel
        # has to give up the autoDefault Qt hands the first button it finds,
        # or the screen offers two blue buttons and therefore none.
        buttons = QDialogButtonBox()
        open_button = QPushButton("Open the terminal")
        open_button.setObjectName("primary")
        open_button.setDefault(True)
        open_button.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.setDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(open_button, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        open_button.setFocus()

    def port(self) -> int:
        return int(self._port.value())


def ask_ssh_port(profile: ServerProfile, parent=None) -> int:
    """The port to shell on, or 0 when the answer was "not now"."""
    dialog = ShellTargetDialog(profile, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 0
    return dialog.port()
