"""A single isolated browser tab with phpMyAdmin auto-login."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from mysql_runner.storage.models import AuthType, ServerProfile
from mysql_runner.ui import theme
from mysql_runner.web.autologin import (
    build_dark_mode_script,
    build_login_script,
    build_startup_script,
)
from mysql_runner.web.profile_factory import create_isolated_profile


class BrowserTab(QWidget):
    """Hosts a QWebEngineView bound to a profile and auto-logs in.

    On every ``loadFinished`` we attempt the cookie-auth form fill. Because
    phpMyAdmin redirects back to the login page when a session times out, this
    same hook transparently re-authenticates after a timeout. Once logged in we
    optionally apply dark mode and run a startup SQL script.

    Passing ``shared_profile`` reuses another tab's engine profile so the clone
    sees the exact same logged-in session (shared cookie jar).
    """

    status_message = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    def __init__(
        self,
        profile: ServerProfile,
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
        shared_profile: QWebEngineProfile | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._dark_mode = dark_mode
        self._startup_done = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if shared_profile is not None:
            self._engine_profile = shared_profile
            self._owns_profile = False
        else:
            # Profile is unparented; the main window manages its lifetime so it
            # can outlive clones that share the same session.
            self._engine_profile = create_isolated_profile(None)
            self._owns_profile = True

        self._page = QWebEnginePage(self._engine_profile, self)
        self._view = QWebEngineView(self)
        # A web view is the one widget here that needs a real window handle of
        # its own, and Qt's default answer is to give one to every ancestor as
        # well - which recreates them, and reads as the whole application
        # blinking the first time a phpMyAdmin tab is opened. This keeps the
        # handle to the view itself, where it is actually needed.
        self._view.setAttribute(
            Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True
        )
        # Chromium paints white until the page has something to show, which on
        # a dark window is a flash of white the size of the tab. Starting it on
        # the application's own background makes the hand-over invisible.
        self._page.setBackgroundColor(
            QColor(theme.palette(dark_mode).bg)
        )
        self._view.setPage(self._page)
        layout.addWidget(self._view)

        # HTTP Basic Auth support.
        if profile.auth_type in (AuthType.AUTO, AuthType.HTTP_BASIC):
            self._page.authenticationRequired.connect(self._on_http_auth)

        self._view.loadFinished.connect(self._on_load_finished)
        self._page.titleChanged.connect(self._on_title_changed)
        self._view.load(QUrl(profile.url))

    # ----- accessors -----------------------------------------------------
    @property
    def server_profile(self) -> ServerProfile:
        return self._profile

    @property
    def engine_profile(self) -> QWebEngineProfile:
        return self._engine_profile

    @property
    def owns_profile(self) -> bool:
        return self._owns_profile

    def current_title(self) -> str:
        title = self._page.title()
        return title or self._profile.label

    # ----- dark mode -----------------------------------------------------
    def set_dark_mode(self, enable: bool) -> None:
        self._dark_mode = enable
        if self._page is not None:
            # Keep the colour behind the page in step too, or the next reload
            # flashes the old theme's background before painting.
            self._page.setBackgroundColor(QColor(theme.palette(enable).bg))
            self._page.runJavaScript(build_dark_mode_script(enable))

    # ----- auth handlers -------------------------------------------------
    def _on_http_auth(self, _url: QUrl, auth) -> None:
        auth.setUser(self._profile.username)
        auth.setPassword(self._profile.password)
        self.status_message.emit(f"Sent HTTP credentials to {self._profile.label}")

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self.status_message.emit(f"Failed to load {self._profile.label}")
            return

        # Apply dark mode on every load (login page included).
        if self._dark_mode:
            self._page.runJavaScript(build_dark_mode_script(True))

        # Try the cookie login form when relevant.
        if self._profile.auth_type in (AuthType.AUTO, AuthType.COOKIE):
            script = build_login_script(self._profile.username, self._profile.password)
            self._page.runJavaScript(script, self._on_login_result)
        else:
            self._maybe_run_startup()

    def _on_login_result(self, submitted: object) -> None:
        if submitted:
            self.status_message.emit(f"Logging in to {self._profile.label}…")
        else:
            # No login form found -> we are already logged in.
            self._maybe_run_startup()

    def _maybe_run_startup(self) -> None:
        if self._startup_done or not self._profile.startup_script.strip():
            return
        self._startup_done = True
        self._page.runJavaScript(build_startup_script(self._profile.startup_script))
        self.status_message.emit(f"Running startup SQL for {self._profile.label}")

    def _on_title_changed(self, title: str) -> None:
        # Let the main window refresh the tab label.
        self.title_changed.emit(title or self._profile.label)

    # ----- teardown ------------------------------------------------------
    def cleanup(self) -> None:
        """Tear down the view/page. The engine profile is owned and released by
        the main window so that cloned sessions sharing it stay valid."""
        try:
            self._view.setPage(None)
        except Exception:
            pass
        if self._page is not None:
            self._page.deleteLater()
        self._page = None
