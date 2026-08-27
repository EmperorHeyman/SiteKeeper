"""Runtime feature flags and facts about the process we are running in.

Compact builds disable the embedded Qt WebEngine browser by default to keep
the one-file executable size down. Set MYSQL_RUNNER_EMBEDDED_BROWSER=1 to
re-enable in-app browser tabs.
"""

from __future__ import annotations

import os


def embedded_browser_enabled() -> bool:
    value = os.getenv("MYSQL_RUNNER_EMBEDDED_BROWSER", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def running_elevated() -> bool:
    """Whether this process holds an elevated (administrator) token.

    Worth knowing because of one Windows behaviour that looks exactly like a
    bug in this app: mapped network drives belong to the *unelevated* logon
    session, so an elevated process cannot see Z: or Y: at all - the letters
    simply do not exist for it, while Explorer shows them perfectly well.
    (Setting EnableLinkedConnections shares them; almost nobody has.)
    """
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def mapped_drive_letter(path: str) -> str:
    """The drive letter of ``path`` when it could be a mapped network one.

    Returns "" for UNC paths (which elevation does not hide) and for the
    system drive, so callers only warn about letters that plausibly went
    missing for the reason above.
    """
    if os.name != "nt" or not path or path.startswith("\\\\"):
        return ""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) != 2 or not drive[0].isalpha():
        return ""
    system = os.path.splitdrive(os.environ.get("SystemRoot", "C:"))[0]
    return "" if drive.upper() == system.upper() else drive.upper()
