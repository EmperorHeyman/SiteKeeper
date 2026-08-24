"""Runtime configuration for the Sitekeeper backend.

The backend runs as a localhost-only sidecar started by the Tauri shell, which
picks a free port and a per-launch shared-secret token and passes both in via
the environment. Run standalone for development and the token check is skipped.

Mirrors the RaplMail backend's configuration shape so the two projects stay
recognisably the same codebase.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend settings, all overridable from the environment."""

    model_config = SettingsConfigDict(env_prefix="MYSQLRUNNER_", extra="ignore")

    #: Port to listen on. 0 lets the caller pick (dev default below).
    port: int = 8766
    #: Per-launch shared secret. Empty disables the header check (dev only).
    token: str = ""
    #: Version string injected by the shell, surfaced on /health.
    version: str = "0.0.0-dev"
    #: Bind address - never anything but loopback.
    host: str = "127.0.0.1"

    @property
    def dev_mode(self) -> bool:
        """True when no token was injected, i.e. not launched by the shell."""
        return not self.token


@lru_cache
def get_settings() -> Settings:
    return Settings()


def data_dir() -> str:
    """Per-user data directory, shared with the PyQt build of the app.

    Deliberately the same folder the Qt app uses, so a vault created by either
    front end opens in the other.
    """
    from mysql_runner.paths import app_data_dir

    return str(app_data_dir())


def allowed_origins() -> list[str]:
    """CORS origins for the webview and the Vite dev server."""
    origins = ["tauri://localhost", "http://tauri.localhost"]
    if get_settings().dev_mode:
        origins += [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    return origins


def env_summary() -> dict[str, object]:
    """Non-secret snapshot of the environment, for the health endpoint."""
    settings = get_settings()
    return {
        "version": settings.version,
        "dev_mode": settings.dev_mode,
        "data_dir": data_dir(),
        "appdata_override": bool(os.environ.get("APPDATA")),
    }
