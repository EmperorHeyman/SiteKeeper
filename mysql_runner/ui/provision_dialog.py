"""The question a claim ticket has to pass through before it becomes a connection.

Three screens in one dialog, because they are one decision taken in stages and
a dialog that closes and reopens twice reads as three unrelated interruptions:

1. **Ask.** Nothing has left the machine yet, so the only thing shown is the
   hostname the link named - which is also the one thing TLS is about to check.
   No product name, no connection count, no logo: everything a link says about
   itself is something a forged link says just as convincingly.
2. **Contacting.** The fetch runs on its own thread. Fifteen seconds of a frozen
   window would be indistinguishable from a crash.
3. **Review.** What the provider actually offered, item by item, before a single
   credential reaches the vault.

There is no "always allow this provider" and there never will be. The dialog is
the entire security boundary on this machine: everything before it is a string
someone else wrote, and everything after it is a saved credential.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.storage import provisioning
from mysql_runner.storage.models import Environment
from mysql_runner.storage.provisioning import Claim, Offer, ProvisioningError
from mysql_runner.ui import theme

_ENV_LABELS = {
    Environment.PROD: "production",
    Environment.STAGING: "staging",
    Environment.DEV: "dev",
}


def _text(value: str, *, name: str = "", mono: bool = False) -> QLabel:
    """A label that shows its text and never renders it.

    Most of the strings on these screens were written by whoever sent the link.
    QLabel defaults to ``AutoText``, which sniffs for markup and renders it - so
    a provider calling itself ``<b>Your Bank</b>`` would get bold, and one
    calling itself ``<img src=…>`` would get a network request out of a dialog
    that has not been agreed to yet. Everything borrowed goes through here.
    """
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    if mono:
        label.setFont(theme.mono_font(9))
    return label


class _FetchThread(QThread):
    """Presents the ticket, off the GUI thread."""

    fetched = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, claim: Claim, parent=None) -> None:
        super().__init__(parent)
        self._claim = claim

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            offer = provisioning.fetch(self._claim)
        except ProvisioningError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # a bug here must not take the app down
            self.failed.emit(f"The handover failed unexpectedly: {exc}")
        else:
            self.fetched.emit(offer)


class ProvisionDialog(QDialog):
    """Ask, fetch, and show what arrived. ``offer()`` holds the answer."""

    def __init__(self, claim: Claim, dark: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._claim = claim
        self._dark = dark
        self._offer: Offer | None = None
        self._thread: _FetchThread | None = None

        self.setWindowTitle("Connections from your hosting provider")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_ask())
        self._pages.addWidget(self._build_busy())
        self._review_host = QWidget()
        self._pages.addWidget(self._review_host)
        layout.addWidget(self._pages)

        self._buttons = QDialogButtonBox()
        self._cancel = QPushButton("Cancel")
        self._cancel.clicked.connect(self.reject)
        self._go = QPushButton("Continue")
        self._go.setObjectName("primary")
        self._go.clicked.connect(self._on_go)
        self._buttons.addButton(self._cancel, QDialogButtonBox.ButtonRole.RejectRole)
        self._buttons.addButton(self._go, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(self._buttons)
        self._go.setFocus()

    # ----- pages ---------------------------------------------------------
    def _build_ask(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        box.addWidget(_text(f"Add connections from {self._claim.source}?", name="title"))

        body = QLabel(
            "Something opened a Sitekeeper handover link. If you just clicked "
            "<b>Open in Sitekeeper</b> in your hosting control panel, this is "
            "that. Continuing asks that host for the connection details it has "
            "for you - over HTTPS, and only after it has proved it really is "
            f"{self._claim.source}. You will see exactly what it offers before "
            "anything is saved."
        )
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        box.addWidget(body)

        where = QFrame()
        where.setObjectName("card")
        where_box = QVBoxLayout(where)
        where_box.addWidget(_text("Sitekeeper will contact", name="hint"))
        endpoint = _text(self._claim.endpoint, mono=True)
        endpoint.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        where_box.addWidget(endpoint)
        box.addWidget(where)

        box.addWidget(_text(
            "If you did not click anything, or you do not recognise that "
            "address, cancel. A link like this can be sent to you by anyone.",
            name="warning",
        ))
        box.addStretch(1)
        return page

    def _build_busy(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        box.addWidget(_text(f"Contacting {self._claim.source}…", name="title"))
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        box.addWidget(bar)
        box.addWidget(_text(
            "Presenting the ticket from the link. These are usually single-use "
            "and expire within minutes, which is why nothing in the link itself "
            "is worth stealing.",
            name="hint",
        ))
        box.addStretch(1)
        return page

    def _build_review(self, offer: Offer) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        count = len(offer.profiles)
        box.addWidget(_text(
            f"{offer.issuer} is offering {count} "
            f"connection{'s' if count != 1 else ''}.",
            name="title",
        ))
        box.addWidget(_text(
            f"Sent by {offer.source}. Adding these saves their usernames and "
            "passwords to your encrypted vault.",
            name="hint",
        ))

        listing = QWidget()
        listing_box = QVBoxLayout(listing)
        listing_box.setContentsMargins(0, 0, 0, 0)
        listing_box.setSpacing(6)
        for profile in offer.profiles:
            listing_box.addWidget(self._row(profile, offer))
        listing_box.addStretch(1)

        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setWidget(listing)
        scroller.setMinimumHeight(min(320, max(96, count * 58)))
        scroller.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        box.addWidget(scroller, 1)

        for note, style in self._notes(offer):
            box.addWidget(_text(note, name=style))
        return page

    def _row(self, profile, offer: Offer) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setSpacing(10)

        icon = QLabel()
        icon.setPixmap(
            theme.kind_icon(profile.kind.value, self._dark, size=18).pixmap(18, 18)
        )
        icon.setFixedWidth(20)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(icon)

        text = QWidget()
        text_box = QVBoxLayout(text)
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        text_box.addWidget(_text(profile.label, name="title"))
        text_box.addWidget(_text(profile.describe_target(), name="hint", mono=True))
        extras = []
        if profile.group:
            extras.append(f"filed under “{profile.group}”")
        if profile.remote_dir:
            extras.append(f"opens in {profile.remote_dir}")
        if profile.id in offer.keys:
            extras.append("brings its own SSH key")
        if extras:
            text_box.addWidget(_text(" · ".join(extras), name="hint"))
        text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(text, 1)

        tag = _ENV_LABELS.get(profile.environment)
        if tag:
            pill = QLabel(tag)
            pill.setObjectName("pill")
            if profile.environment == Environment.PROD:
                pill.setProperty("state", "fail")
            elif profile.environment == Environment.STAGING:
                pill.setProperty("state", "busy")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
        return card

    def _notes(self, offer: Offer) -> list[tuple[str, str]]:
        """The lines under the list, and how loudly each should be said.

        Only one of these is worth red. A provider that sent a field this app
        refuses from a link has done something worth reporting to them; an
        entry that would not parse, or a connection marked production, is
        information. Painting all three red would make the one that matters
        indistinguishable from the two that do not.
        """
        notes: list[tuple[str, str]] = []
        if offer.dropped:
            reasons = ", ".join(
                f"{name} ({provisioning.REFUSED_FIELDS[name]})"
                for name in offer.dropped
            )
            notes.append((
                "Your provider sent settings Sitekeeper never accepts from a "
                f"link: {reasons}. They were ignored - nothing here can run "
                "anything on this PC - but it is worth telling them.",
                "warning",
            ))
        if offer.skipped:
            shown = "; ".join(offer.skipped[:3])
            more = f" (and {len(offer.skipped) - 3} more)" if len(offer.skipped) > 3 else ""
            notes.append((f"Some entries could not be read: {shown}{more}", "hint"))
        if any(x.environment == Environment.PROD for x in offer.profiles):
            notes.append((
                "Connections marked production get a red tab, so you always "
                "know where you are.",
                "hint",
            ))
        return notes

    # ----- flow ----------------------------------------------------------
    def _on_go(self) -> None:
        if self._offer is not None:
            self.accept()
            return
        self._pages.setCurrentIndex(1)
        self._go.setEnabled(False)
        self._go.setText("Contacting…")
        thread = _FetchThread(self._claim, self)
        thread.fetched.connect(self._on_fetched)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(self._on_thread_done)
        self._thread = thread
        thread.start()

    def _on_fetched(self, offer: object) -> None:
        if not isinstance(offer, Offer):  # pragma: no cover - defensive
            self._on_failed("The provider's answer could not be read.")
            return
        self._offer = offer
        review = self._build_review(offer)
        self._pages.removeWidget(self._review_host)
        self._review_host.deleteLater()
        self._review_host = review
        self._pages.addWidget(review)
        self._pages.setCurrentWidget(review)
        count = len(offer.profiles)
        self._go.setEnabled(True)
        self._go.setText(f"Add {count} connection{'s' if count != 1 else ''}")
        # Enter must stop meaning "yes" here: a customer who pressed it to get
        # past the first screen should have to look at the list before the
        # credentials land. So neither button is the default one any more -
        # Enter does nothing, Space presses whatever is focused, and Escape
        # still cancels. Both have to give up autoDefault for that: Qt makes a
        # focused autoDefault button *the* default button, which the button-box
        # stylesheet paints blue - and a screen with two blue buttons has none.
        self._go.setAutoDefault(False)
        self._cancel.setAutoDefault(False)
        self._cancel.setFocus()
        self.adjustSize()

    def _on_failed(self, message: str) -> None:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        box.addWidget(_text("That did not work", name="title"))
        box.addWidget(_text(provisioning.clean_text(message, limit=400)))
        box.addWidget(_text(
            f"Nothing was saved. The link named {self._claim.source}.", name="hint"
        ))
        box.addStretch(1)
        self._pages.removeWidget(self._review_host)
        self._review_host.deleteLater()
        self._review_host = page
        self._pages.addWidget(page)
        self._pages.setCurrentWidget(page)
        self._go.setVisible(False)
        self._cancel.setText("Close")
        self._cancel.setAutoDefault(False)
        self._cancel.setFocus()

    def _on_thread_done(self) -> None:
        self._thread = None

    def reject(self) -> None:
        # A fetch already in flight cannot be called back - the ticket may well
        # be spent by now - but its result must not reopen a dialog the user
        # has closed, so the signals go first.
        thread = self._thread
        if thread is not None:
            try:
                thread.fetched.disconnect()
                thread.failed.disconnect()
            except TypeError:
                pass
        super().reject()

    def offer(self) -> Offer | None:
        """What the provider offered, once the dialog has been accepted."""
        return self._offer


def ask(claim: Claim, dark: bool = True, parent=None) -> Offer | None:
    """Run the whole handover. Returns the accepted offer, or None."""
    dialog = ProvisionDialog(claim, dark, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.offer()
