"""What you typed on this server last time, and the week before that.

A shell you use twice a day is mostly the same twenty commands, and every one
of them was already typed once. PuTTY forgets them when the window closes; so
did this, because the history lived in a list on a widget. It lives on disk
now, one file per connection, so the deploy command you worked out on Friday
is one Up-arrow away on Monday - and so that a suggestion can appear as you
type, which is the part that actually saves the typing.

Per connection rather than global on purpose. "The last thing I ran here" is a
useful question; "the last thing I ran anywhere" is not, and mixing two servers'
histories is how a command meant for staging gets recalled on production.

Two things are deliberately *not* remembered:

* anything typed with a leading space, which is the convention every shell uses
  for "do this but do not write it down", and
* anything that looks like it carries a password on the command line, because
  an application that keeps an encrypted vault has no business leaving
  ``mysql -pSECRET`` in a plain text file next to it.

Qt-free: the terminal turns this into keystrokes, and the tests use it directly.
"""

from __future__ import annotations

import os
import re

from mysql_runner.paths import app_data_dir

#: Commands kept per connection. Long enough to cover months of real use,
#: short enough that the file is read and rewritten in no time at all.
MAX_ENTRIES = 2000

#: Command lines that carry a secret in plain sight. Deliberately a little
#: eager: failing to remember a command costs one retype, and remembering a
#: password costs rather more.
_SECRETS = re.compile(
    r"""
      (?:^|\s)-p\S                      # mysql -pSECRET
    | --password[=\s]\S                 # --password=SECRET
    | (?:^|\s)(?:PG)?PASSWORD=          # PASSWORD=... as an env prefix
    | (?:^|\s)-u\s*\S+:\S               # curl -u user:pass
    | --token[=\s]\S                    # --token=...
    | (?:^|\s)\w*(?:PASS|SECRET|TOKEN|APIKEY)\w*=\S
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: Why a command was not written down, for the status line.
SKIPPED_SPACE = "not remembered (it started with a space)"
SKIPPED_SECRET = "not remembered (it looks like it contains a password)"


def history_dir():
    """Where the per-connection history files live."""
    return app_data_dir() / "shell_history"


def _safe_name(profile_id: str) -> str:
    """A filename from a profile id, defensively - ids are hex, but still."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", profile_id or "")
    return cleaned or "unknown"


class ShellHistory:
    """One connection's command history, loaded once and saved as it grows."""

    def __init__(self, profile_id: str, *, limit: int = MAX_ENTRIES) -> None:
        self._path = history_dir() / f"{_safe_name(profile_id)}.txt"
        self._limit = max(1, limit)
        self._entries: list[str] = []
        self._load()

    # ----- persistence ----------------------------------------------------
    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return  # no history yet, or unreadable: start empty
        self._entries = [line for line in text.splitlines() if line.strip()]
        if len(self._entries) > self._limit:
            self._entries = self._entries[-self._limit:]

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text("\n".join(self._entries) + "\n", encoding="utf-8")
            os.replace(temp, self._path)
        except OSError:
            # History is a convenience. Failing to write it must never stop
            # somebody using the shell.
            pass

    # ----- adding ---------------------------------------------------------
    def add(self, command: str) -> str:
        """Remember a command. Returns "" when kept, or why it was not.

        A repeat is moved to the end rather than duplicated, so "the last time
        I used this" is what ranks it - which is what makes the suggestion in
        front of the cursor the right one more often than not.
        """
        if not command.strip():
            return ""
        if command[:1].isspace():
            return SKIPPED_SPACE
        if _SECRETS.search(command):
            return SKIPPED_SECRET
        command = command.rstrip()
        if command in self._entries:
            self._entries.remove(command)
        self._entries.append(command)
        if len(self._entries) > self._limit:
            self._entries = self._entries[-self._limit:]
        self._save()
        return ""

    def clear(self) -> None:
        """Forget everything for this connection."""
        self._entries = []
        try:
            self._path.unlink()
        except OSError:
            pass

    # ----- reading --------------------------------------------------------
    def entries(self) -> list[str]:
        """Oldest first, the way the file reads."""
        return list(self._entries)

    def matching(self, prefix: str) -> list[str]:
        """Commands starting with ``prefix``, most recently used first.

        This is what makes Up different from PuTTY's: type ``git`` and Up
        walks the git commands, not everything that happened to come before.
        An empty prefix walks the lot, which is the familiar behaviour.
        """
        if not prefix:
            return list(reversed(self._entries))
        return [
            entry for entry in reversed(self._entries) if entry.startswith(prefix)
        ]

    def suggest(self, text: str) -> str:
        """The rest of the most recent command starting with ``text``.

        Returns only the *tail*, which is what gets drawn in front of the
        cursor - the caller never has to work out how much of the suggestion
        is already typed.
        """
        if not text or text[-1:].isspace() and text.strip() == "":
            return ""
        for entry in reversed(self._entries):
            if entry.startswith(text) and len(entry) > len(text):
                return entry[len(text):]
        return ""

    def search(self, needle: str) -> list[str]:
        """Commands containing ``needle``, most recent first - for Ctrl+R."""
        if not needle:
            return []
        lowered = needle.lower()
        return [
            entry for entry in reversed(self._entries) if lowered in entry.lower()
        ]

    def commands(self) -> list[str]:
        """The distinct first words used here, for completing a command name."""
        seen: list[str] = []
        for entry in reversed(self._entries):
            first = entry.strip().split(" ", 1)[0]
            if first and first not in seen:
                seen.append(first)
        return seen
