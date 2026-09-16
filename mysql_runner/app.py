"""Application bootstrap: unlock the vault, then show the main window."""

from __future__ import annotations

import getpass
import hashlib
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

# Import WebEngine before QApplication construction for embedded browser tabs.
from PyQt6 import QtWebEngineWidgets  # noqa: F401
from PyQt6.QtWidgets import QApplication, QMessageBox

from mysql_runner.crypto import dpapi
from mysql_runner.crypto import vault as vault_mod
from mysql_runner.paths import resource_path
from mysql_runner.storage import provisioning
from mysql_runner.storage.settings import Settings
from mysql_runner.storage.store import ServerStore, StoreError, opens_store
from mysql_runner.ui.idle_watcher import IdleWatcher
from mysql_runner.ui import theme
from mysql_runner.ui.main_window import MainWindow
from mysql_runner.ui.master_password_dialog import (
    CreateMasterPasswordDialog,
    UnlockDialog,
)


def _create_vault() -> vault_mod.Vault | None:
    """First-run flow: pick a master password, or opt out of one."""
    dialog = CreateMasterPasswordDialog(allow_no_password=dpapi.is_available())
    if not dialog.exec():
        return None
    if dialog.use_password():
        return vault_mod.initialize(dialog.password())
    try:
        return vault_mod.initialize_keyless()
    except vault_mod.VaultError as exc:
        QMessageBox.critical(None, "Could not create the vault", str(exc))
        return None


def effective_idle_minutes(settings: Settings) -> int:
    """Idle auto-lock timeout to arm, in minutes (0 disables it).

    Auto-locking is pointless without a master password: re-unlocking would be
    instant, so the window would only flicker away and come straight back.
    """
    if vault_mod.is_initialized() and not vault_mod.requires_password():
        return 0
    return settings.effective_idle_lock_minutes()


def _unlock_vault(use_keyring: bool = True) -> vault_mod.Vault | None:
    """Run the first-run / unlock flow, returning an open Vault or None.

    When ``use_keyring`` is False the cached key is ignored and the master
    password is always requested (used to honour "ask for password at start").
    Vaults with password protection turned off open straight away.
    """
    if not vault_mod.is_initialized():
        return _create_vault()

    # Password protection turned off: the key is sealed to the Windows account.
    if not vault_mod.requires_password():
        try:
            return vault_mod.unlock_keyless()
        except vault_mod.VaultError as exc:
            QMessageBox.critical(None, "Could not unlock the vault", str(exc))
            return None

    # Try the keyring cache first (unless the caller wants a password prompt).
    # A cached key that cannot open the store is stale - the cache is keyed by
    # application name, not by vault file - so drop it and ask for the password
    # rather than failing the whole launch.
    if use_keyring:
        vault = vault_mod.unlock_with_keyring()
        if vault is not None:
            if opens_store(vault):
                return vault
            vault_mod.clear_keyring_cache()

    # Fall back to the master password (allow a few attempts).
    for _ in range(3):
        dialog = UnlockDialog()
        if not dialog.exec():
            return None
        try:
            return vault_mod.unlock_with_password(dialog.password())
        except vault_mod.InvalidMasterPassword:
            QMessageBox.warning(
                None, "Incorrect password", "That master password is incorrect."
            )
    return None


def _claim_argument(arguments: list[str]) -> str:
    """The handover link or ``.skc`` path the app was started on, if any.

    Windows hands a registered scheme over as a plain argument, and a double
    -clicked file the same way, so both arrive here looking like the other.
    """
    for argument in arguments:
        if argument.startswith("-"):
            continue
        if provisioning.looks_like_claim(argument):
            return argument.strip().strip('"')
    return ""


def _claim_socket_name() -> str:
    """Name of the pipe a running instance listens on, per Windows account.

    Hashed rather than spelled out: the name is visible to everything on the
    machine, and a username is not worth publishing to learn nothing.
    """
    digest = hashlib.sha256(getpass.getuser().encode("utf-8", "replace")).hexdigest()
    return f"sitekeeper-claim-{digest[:16]}"


def _hand_to_running_instance(claim_text: str) -> bool:
    """Give an already-running Sitekeeper the claim. True when it took it.

    A second copy of the app would open its own vault prompt and its own
    window, which is the wrong answer to "add this connection to the list I am
    already looking at". Only claims are forwarded: launching the app twice on
    purpose still gives you two windows, as it always has.
    """
    socket = QLocalSocket()
    socket.connectToServer(_claim_socket_name())
    if not socket.waitForConnected(800):
        return False
    socket.write(claim_text.encode("utf-8"))
    socket.flush()
    delivered = socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    return delivered


def _listen_for_claims(deliver) -> QLocalServer | None:
    """Accept claims forwarded by later launches. None when another app owns it."""
    name = _claim_socket_name()
    server = QLocalServer()
    # A pipe left behind by a crash would otherwise make every later launch
    # believe an instance is running and refuse to listen for the rest of the
    # session. Probing first keeps us from stealing a live one.
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.disconnectFromServer()
        return None
    QLocalServer.removeServer(name)
    if not server.listen(name):
        return None

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def read() -> None:
            payload = bytes(connection.readAll()).decode("utf-8", "replace")
            connection.disconnectFromServer()
            text = payload.strip()
            if text:
                deliver(text)

        connection.readyRead.connect(read)
        connection.disconnected.connect(connection.deleteLater)

    server.newConnection.connect(on_connection)
    return server


def run() -> int:
    QGuiApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("Sitekeeper")

    # Forwarding happens before anything is unlocked: if another instance is
    # up, this process has nothing to do but hand the link over and go.
    claim_text = _claim_argument(sys.argv[1:])
    if claim_text and _hand_to_running_instance(claim_text):
        return 0

    icon_file = resource_path("icon.ico")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    # Make Windows treat this as its own app (correct taskbar icon/grouping).
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "RAPLGroup.Sitekeeper.1"
            )
        except Exception:
            pass

    settings = Settings.load()
    # One stylesheet for the whole application, so tabs, dialogs and tables
    # match instead of each carrying its own idea of the theme.
    app.setStyle("Fusion")
    app.setStyleSheet(theme.app_stylesheet(settings.dark_mode))
    window_holder: dict[str, MainWindow] = {}
    lock_holder: dict[str, object] = {}

    # Application-wide idle watcher that auto-locks after inactivity.
    idle_watcher = IdleWatcher(effective_idle_minutes(settings))
    app.installEventFilter(idle_watcher)
    idle_watcher.idle.connect(lambda: _invoke(lock_holder.get("on_lock")))

    def on_settings_changed() -> None:
        # Re-arm the idle watcher whenever the timeout preference changes.
        idle_watcher.set_timeout(effective_idle_minutes(settings))

    def deliver_claim(text: str) -> None:
        """Put a handover link in front of whichever window is open now.

        Parsing happens here rather than in the socket reader so that a
        malformed link forwarded by another launch says so, instead of being
        dropped into silence on a window the user is looking at.
        """
        window = window_holder.get("window")
        if window is None:
            # Locked, or between sessions. Asking a user to review connections
            # they cannot yet see saved is worse than asking them to click the
            # link again once they are in.
            QMessageBox.information(
                None,
                "Sitekeeper is locked",
                "Unlock Sitekeeper first, then use the handover link again.",
            )
            return
        try:
            claim = provisioning.load_claim(text)
        except provisioning.ProvisioningError as exc:
            QMessageBox.critical(window, "That link cannot be used", str(exc))
            return
        window.handle_claim(claim)

    def start_session(*, first_launch: bool = False) -> bool:
        # Honour "ask for password at start" only on the initial launch; an
        # in-session re-lock still uses the keyring (if it wasn't cleared).
        use_keyring = not (first_launch and settings.prompt_on_start())
        vault = _unlock_vault(use_keyring)
        if vault is None:
            return False
        try:
            store = ServerStore(vault)
        except StoreError as exc:
            QMessageBox.critical(None, "Vault error", str(exc))
            return False

        def on_lock() -> None:
            idle_watcher.stop()
            vault.lock()
            # Keep the cached key only when the user opted to be remembered
            # (or chose to stay logged in).
            if not settings.keep_password_cached():
                vault_mod.clear_keyring_cache()
            old = window_holder.pop("window", None)
            if old is not None:
                old.close()
            if not start_session():
                app.quit()

        window = MainWindow(
            store, settings, on_lock=on_lock, on_settings_changed=on_settings_changed
        )
        window_holder["window"] = window
        lock_holder["on_lock"] = on_lock
        window.show()
        # (Re)start the idle countdown for the new session.
        idle_watcher.set_timeout(effective_idle_minutes(settings))
        return True

    if not start_session(first_launch=True):
        return 0

    # Held for the process's lifetime: a QLocalServer that goes out of scope
    # stops listening, and later launches would quietly open second windows.
    claim_server = _listen_for_claims(deliver_claim)

    if claim_text:
        # After the event loop is running and the window is up, so the review
        # dialog has a parent to be modal to.
        QTimer.singleShot(0, lambda: deliver_claim(claim_text))

    code = app.exec()
    if claim_server is not None:
        claim_server.close()
    return code


def _invoke(callback) -> None:
    if callable(callback):
        callback()
