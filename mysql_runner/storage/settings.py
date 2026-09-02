"""Plain-JSON UI settings (non-sensitive preferences only).

These preferences do not contain credentials, so they are stored unencrypted:
dark-mode toggle, sidebar layout, split-view state, the idle auto-lock timeout,
and the master-password prompt policy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from mysql_runner.paths import settings_path

#: Sidebar width used when no width has been remembered yet.
DEFAULT_SIDEBAR_WIDTH = 280
#: Narrowest remembered sidebar width, so it can always be grabbed again.
MIN_SIDEBAR_WIDTH = 140
#: Parallel transfer connections per tab, and the ceiling the UI allows.
#: See pool.DEFAULT_WORKERS for why six: a deploy of small files is limited by
#: round trips, not bandwidth, so its time divides by this number.
DEFAULT_TRANSFER_WORKERS = 6
#: What that default used to be. A settings file still holding exactly this is
#: treated as never having chosen - see _sane_workers.
PREVIOUS_TRANSFER_WORKERS = 3
MAX_TRANSFER_WORKERS = 16
#: Highest speed limit the UI offers, in kilobytes per second. Past this the
#: limit stops being a limit on any link somebody would want to set one on.
MAX_TRANSFER_RATE_KB = 1024 * 1024


@dataclass
class Settings:
    """User-interface preferences."""

    # The application's own chrome: window, tabs, tables, dialogs.
    dark_mode: bool = True
    # Dark mode *injected into phpMyAdmin pages* by the bundled Dark Reader.
    # Separate on purpose: a dark app with a light phpMyAdmin (or the reverse)
    # is a perfectly reasonable thing to want, and one switch could not say so.
    web_dark_mode: bool = True
    sidebar_visible: bool = True
    # Collapsed to the icon rail: the sidebar's contents are hidden but a slim
    # strip with an expand button stays put, so there is always a way back.
    sidebar_collapsed: bool = False
    # Width to restore when the sidebar is expanded again.
    sidebar_width: int = DEFAULT_SIDEBAR_WIDTH
    # Second tab pane shown beside the first (side-by-side view).
    split_view: bool = False
    # Pixel widths of the two tab panes while split view is on.
    split_sizes: list[int] = field(default_factory=list)
    idle_lock_minutes: int = 15  # 0 disables auto-lock.
    # Prompt for the master password on every app launch (ignore the keyring
    # cache at startup). The keyring is still used for in-session re-unlocks.
    ask_password_on_start: bool = False
    # Keep the password cached after locking so unlocking never prompts again.
    remember_password: bool = False
    # "Stay logged in": unlock once, then never auto-lock and never re-prompt.
    # This is a convenience preset that overrides the three options above while
    # enabled; a real master password is still kept and the vault stays
    # encrypted. See the effective_* helpers below.
    stay_logged_in: bool = False

    # ----- file transfer -------------------------------------------------
    # Parallel connections per file-manager tab. One is the old behaviour.
    transfer_workers: int = DEFAULT_TRANSFER_WORKERS
    # Ceiling on the combined speed of those connections, in kilobytes per
    # second; 0 is no limit and is the default. It applies to the pool as a
    # whole rather than to each connection, so raising the connection count
    # does not quietly raise the ceiling with it.
    transfer_rate_kb: int = 0
    # Upload to a scratch name and rename into place, so a half-written file
    # is never served to a live request.
    atomic_uploads: bool = True
    # Keep the previous version of anything overwritten, so it can be undone.
    shadow_backups: bool = True
    # How long those saved copies are kept.
    history_days: int = 30
    # Re-read each upload and compare digests. Slow, and sometimes worth it.
    verify_uploads: bool = False
    # Give each uploaded file the local file's modified time. One round trip
    # per file - a seventh of a small-file deploy - for dates on the server
    # that match the ones on this machine. It is no longer needed for
    # correctness now that syncs compare content rather than timestamps.
    preserve_times: bool = True
    # Apply .deployignore / .gitignore to batch transfers and comparisons.
    use_ignore_rules: bool = True
    # Add the built-in list (node_modules, vendor, .git, ...) to those rules.
    ignore_defaults: bool = True
    # Walk folders to show their real size and newest content date.
    folder_stats: bool = True
    # Compare synced folders by content rather than by size and timestamp.
    # A local timestamp only means "when this content was written" until git
    # touches the file: a clone, a pull or a checkout stamps everything it
    # writes with the current time, so identical bytes read as changed and
    # every sync re-uploads the whole tree. Hashing is slower - though on a
    # server with a shell the whole remote side is one command - and it is
    # the only answer that is true on every machine.
    sync_compare_hashes: bool = True
    # Keep both panes on matching directories.
    mirror_navigation: bool = False
    # Ask twice before anything destructive on a production connection.
    production_guard: bool = True
    # Connections whose production warning has been switched off from the
    # warning itself ("Don't ask again for this connection"). Per connection
    # rather than global: the whole point of the guard is that it fires on the
    # servers that matter, and someone who deploys to one production site all
    # day should not have to disarm it for the others too.
    production_guard_off: list[str] = field(default_factory=list)
    # Upload changed files automatically while a tab is watching.
    watch_autosync: bool = False
    # Preferred external terminal ("" picks the first one found).
    terminal_program: str = ""
    # Hand the password to that terminal. It becomes visible to anything that
    # can list processes on this machine, so it is a deliberate choice.
    terminal_send_password: bool = True
    # Preferred code editor for "Open in VS Code" ("" picks the first one
    # found). A name from transfer/editors.py, not a path: the path moves with
    # every update of a per-user install, the name does not.
    editor_program: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()
        # A file written before the app/web split had one shared flag whose
        # default was "off". There is no way to tell a deliberate "off" from an
        # untouched default in those files, so they are treated as never having
        # chosen and get the new dark default; whatever is saved after that
        # sticks.
        chose_before = "web_dark_mode" in data
        app_dark = bool(data.get("dark_mode", True)) if chose_before else True
        return cls(
            dark_mode=app_dark,
            web_dark_mode=bool(data.get("web_dark_mode", app_dark)),
            sidebar_visible=bool(data.get("sidebar_visible", True)),
            sidebar_collapsed=bool(data.get("sidebar_collapsed", False)),
            sidebar_width=cls._sane_width(data.get("sidebar_width")),
            split_view=bool(data.get("split_view", False)),
            split_sizes=cls._sane_sizes(data.get("split_sizes")),
            idle_lock_minutes=int(data.get("idle_lock_minutes", 15)),
            ask_password_on_start=bool(data.get("ask_password_on_start", False)),
            remember_password=bool(data.get("remember_password", False)),
            stay_logged_in=bool(data.get("stay_logged_in", False)),
            transfer_workers=cls._sane_workers(data.get("transfer_workers")),
            transfer_rate_kb=cls._sane_rate(data.get("transfer_rate_kb")),
            atomic_uploads=bool(data.get("atomic_uploads", True)),
            shadow_backups=bool(data.get("shadow_backups", True)),
            history_days=max(0, int(data.get("history_days", 30) or 0)),
            verify_uploads=bool(data.get("verify_uploads", False)),
            preserve_times=bool(data.get("preserve_times", True)),
            use_ignore_rules=bool(data.get("use_ignore_rules", True)),
            ignore_defaults=bool(data.get("ignore_defaults", True)),
            folder_stats=bool(data.get("folder_stats", True)),
            sync_compare_hashes=bool(data.get("sync_compare_hashes", True)),
            mirror_navigation=bool(data.get("mirror_navigation", False)),
            production_guard=bool(data.get("production_guard", True)),
            production_guard_off=cls._sane_ids(data.get("production_guard_off")),
            watch_autosync=bool(data.get("watch_autosync", False)),
            terminal_program=str(data.get("terminal_program", "") or ""),
            terminal_send_password=bool(data.get("terminal_send_password", True)),
            editor_program=str(data.get("editor_program", "") or ""),
        )

    # ----- validation -----------------------------------------------------
    @staticmethod
    def _sane_width(value: object) -> int:
        """Clamp a remembered sidebar width so it can never become unusable."""
        try:
            width = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_SIDEBAR_WIDTH
        return max(MIN_SIDEBAR_WIDTH, width)

    @staticmethod
    def _sane_workers(value: object) -> int:
        """Clamp the connection count; zero or nonsense means the default.

        A file holding exactly the *old* default gets the new one. There is no
        way to tell a deliberate three from an untouched three, and the number
        was only ever three because it was cautious - it is the single biggest
        thing deciding how long a deploy of small files takes, and leaving
        every existing installation on the slow value would mean the people
        who already noticed the problem are the ones who never see the fix.
        Anyone who really wants three sets it again and it sticks, because
        what is saved after this is no longer the old default.
        """
        try:
            count = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_TRANSFER_WORKERS
        if count == PREVIOUS_TRANSFER_WORKERS:
            return DEFAULT_TRANSFER_WORKERS
        return max(1, min(MAX_TRANSFER_WORKERS, count))

    @staticmethod
    def _sane_rate(value: object) -> int:
        """Clamp the speed limit; anything unreadable means no limit."""
        try:
            rate = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return max(0, min(MAX_TRANSFER_RATE_KB, rate))

    @staticmethod
    def _sane_ids(value: object) -> list[str]:
        """A list of profile ids, ignoring anything that is not one."""
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str) and item]

    @staticmethod
    def _sane_sizes(value: object) -> list[int]:
        """Accept only a pair of positive pane widths; otherwise fall back."""
        if not isinstance(value, list) or len(value) != 2:
            return []
        try:
            sizes = [int(v) for v in value]
        except (TypeError, ValueError):
            return []
        return sizes if all(s > 0 for s in sizes) else []

    # ----- effective behaviour -------------------------------------------
    # "Stay logged in" takes precedence over the granular options so the app
    # only ever has to consult these three helpers.
    def effective_idle_lock_minutes(self) -> int:
        """Idle timeout in minutes; 0 (never) while staying logged in."""
        return 0 if self.stay_logged_in else self.idle_lock_minutes

    def keep_password_cached(self) -> bool:
        """Whether the cached key should survive a lock (no re-prompt)."""
        return self.stay_logged_in or self.remember_password

    def prompt_on_start(self) -> bool:
        """Whether to ignore the keyring and ask for the password at launch."""
        return self.ask_password_on_start and not self.stay_logged_in

    def save(self) -> None:
        settings_path().write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )
