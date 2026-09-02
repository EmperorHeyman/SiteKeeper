"""The one question SSH exists to ask, put once, in words.

The first time you connect to a server there is nothing to check its identity
against - no certificate authority, no chain of trust, just a key it presents
and your judgement about whether it is the right machine. Every ssh client
stops here and shows you a fingerprint. Sitekeeper used to record whatever
answered and say nothing, which quietly turned the one check SSH has into no
check at all.

So it asks. The wording matters more than the dialog does: "SHA256:2ip..." is
not a question anybody can answer, so it says what it is *for* - compare this
with what your host shows you - and it defaults to No. Both fingerprint forms
are shown, because the value you have to compare against is often a hosting
control panel that still prints MD5, and a fingerprint in a format you cannot
find is no better than none.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from mysql_runner.transfer import hostkeys
from mysql_runner.ui import theme


class HostKeyDialog(QDialog):
    """Show a server's fingerprint and ask whether to trust it from now on."""

    def __init__(self, unknown: hostkeys.HostKeyUnknown, parent=None) -> None:
        super().__init__(parent)
        self._unknown = unknown
        self.setWindowTitle("Is this the right server?")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        where = unknown.host
        if unknown.port != hostkeys.DEFAULT_PORT:
            where += f" port {unknown.port}"
        headline = QLabel(f"You have not connected to {where} before.")
        headline.setObjectName("title")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        body = QLabel(
            "SSH cannot look a server up and confirm it, so this first "
            "connection is the one that decides. Compare the fingerprint below "
            "with the one your host publishes - in their control panel, their "
            "welcome email, or from the server itself. If it matches, "
            "Sitekeeper will recognise this server from now on."
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(body)

        prints = QFrame()
        prints.setObjectName("card")
        print_layout = QVBoxLayout(prints)
        bits = f" · {unknown.bits} bits" if unknown.bits else ""
        kind = QLabel(f"{unknown.key_type}{bits}")
        kind.setObjectName("hint")
        print_layout.addWidget(kind)
        for value in (unknown.sha256, unknown.md5):
            line = QLabel(value)
            line.setFont(theme.mono_font())
            line.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            line.setWordWrap(True)
            print_layout.addWidget(line)
        layout.addWidget(prints)

        warning = QLabel(
            "If it does not match, something is answering in your server's "
            "place. Say no and find out why before you connect."
        )
        warning.setObjectName("warning")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        buttons = QDialogButtonBox()
        # Neither button is styled as the loud one. Everywhere else in the app
        # the blue button is the thing you came to do; here the thing you came
        # to do is *read the fingerprint*, and painting "yes" blue would be
        # colour arguing for the answer with the worse failure. Enter presses
        # "no", and the text is what decides the rest.
        self._trust = QPushButton("Yes, this is my server")
        self._trust.setAutoDefault(False)
        self._trust.clicked.connect(self.accept)
        cancel = QPushButton("No, don't connect")
        cancel.setAutoDefault(True)
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self._trust, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(buttons)
        cancel.setFocus()

    def remember(self) -> None:
        """Record the key, so this is asked once and not once per connection."""
        hostkeys.trust(self._unknown.host, self._unknown.port, self._unknown.key)


def ask(unknown: hostkeys.HostKeyUnknown, parent=None) -> bool:
    """Put the question. True when the key was accepted and recorded."""
    dialog = HostKeyDialog(unknown, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    dialog.remember()
    return True
