"""Application paths (per-user AppData)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Sitekeeper"

#: What the application was called before 1.3.0. Everything it keeps lives in
#: one directory, so the rename has to bring that directory along or a user
#: would open the new build to an empty connection list and no vault.
LEGACY_APP_NAME = "MySQLRunner"


def resource_path(name: str) -> Path:
    """Return the path to a bundled resource.

    Works both in development and inside a PyInstaller build, where data files
    are unpacked to ``sys._MEIPASS``.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / name
    # Project root (two levels up from this file: mysql_runner/paths.py).
    return Path(__file__).resolve().parent.parent / name


def app_data_dir() -> Path:
    """Return the per-user application data directory, creating it if needed.

    The first time the new directory is wanted, an old MySQL Runner one is
    moved across: the vault, the connections, the settings, the sync rules, the
    known hosts and the shadow backups are all in there. A rename is atomic on
    one volume and leaves nothing behind to go stale; where it cannot be done
    the contents are copied and the original left alone, because a duplicate is
    a much smaller problem than a half-moved vault.
    """
    base = os.environ.get("APPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / ".config"
    path = root / APP_NAME
    if not path.exists():
        _adopt_legacy_dir(root / LEGACY_APP_NAME, path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _adopt_legacy_dir(old: Path, new: Path) -> None:
    """Bring a pre-rename data directory over to the new name."""
    if not old.is_dir():
        return
    try:
        old.rename(new)
        return
    except OSError:
        pass  # in use, or on another volume: fall back to copying
    try:
        shutil.copytree(old, new, dirs_exist_ok=True)
    except (OSError, shutil.Error):
        pass  # starting empty is bad; failing to start at all is worse


def vault_path() -> Path:
    """Path to the encrypted key vault metadata file."""
    return app_data_dir() / "vault.json"


def servers_path() -> Path:
    """Path to the encrypted server profiles file."""
    return app_data_dir() / "servers.enc"


def settings_path() -> Path:
    """Path to the (plain JSON) UI settings file."""
    return app_data_dir() / "settings.json"


def sync_rules_path() -> Path:
    """Path to the (plain JSON) folder sync rules."""
    return app_data_dir() / "sync_rules.json"


def known_hosts_path() -> Path:
    """Path to the SSH known-hosts file used by SFTP connections."""
    return app_data_dir() / "known_hosts"


def mcp_policy_path() -> Path:
    """Path to the (plain JSON) grants the MCP server reads on every call.

    Kept out of settings.json deliberately. This is a permission grant, not a
    preference: it should not be reset by anything that resets the interface,
    and "what may Claude do" is worth being able to read, back up and diff on
    its own.
    """
    return app_data_dir() / "mcp_policy.json"
