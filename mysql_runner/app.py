"""Application bootstrap: unlock the vault, then show the main window."""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QIcon

# Import WebEngine before QApplication construction for embedded browser tabs.
from PyQt6 import QtWebEngineWidgets  # noqa: F401
from PyQt6.QtWidgets import QApplication, QMessageBox

from mysql_runner.crypto import dpapi
from mysql_runner.crypto import vault as vault_mod
from mysql_runner.paths import resource_path
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


def run() -> int:
    QGuiApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("Sitekeeper")

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

    return app.exec()


def _invoke(callback) -> None:
    if callable(callback):
        callback()
