"""Back / forward navigation memory for a file pane.

A browser-style stack: visiting a directory pushes it, Back walks towards
older entries without forgetting the newer ones, and visiting something new
while parked in the middle discards the forward tail. Kept free of Qt so it can
be tested on its own and reused by both front ends.
"""

from __future__ import annotations

import posixpath

#: How many directories to remember per pane.
LIMIT = 200


class NavHistory:
    """The visited-directory stack for one pane."""

    def __init__(self, limit: int = LIMIT) -> None:
        self._entries: list[str] = []
        self._index = -1
        self._limit = max(2, limit)

    # ----- state ----------------------------------------------------------
    @property
    def current(self) -> str:
        if 0 <= self._index < len(self._entries):
            return self._entries[self._index]
        return ""

    @property
    def entries(self) -> list[str]:
        return list(self._entries)

    @property
    def index(self) -> int:
        return self._index

    def can_go_back(self) -> bool:
        return self._index > 0

    def can_go_forward(self) -> bool:
        return -1 < self._index < len(self._entries) - 1

    def recent(self, count: int = 10) -> list[str]:
        """The most recently visited directories, newest first, deduplicated."""
        seen: list[str] = []
        for path in reversed(self._entries[: self._index + 1]):
            if path not in seen:
                seen.append(path)
            if len(seen) >= count:
                break
        return seen

    # ----- movement -------------------------------------------------------
    def visit(self, path: str) -> None:
        """Record a directory the user navigated to."""
        if not path:
            return
        if self.current == path:
            return  # A refresh is not a navigation.
        del self._entries[self._index + 1:]
        self._entries.append(path)
        if len(self._entries) > self._limit:
            # Drop the oldest; the index moves with it.
            overflow = len(self._entries) - self._limit
            del self._entries[:overflow]
        self._index = len(self._entries) - 1

    def back(self) -> str:
        """Step back one entry and return it ("" when there is none)."""
        if not self.can_go_back():
            return ""
        self._index -= 1
        return self._entries[self._index]

    def forward(self) -> str:
        """Step forward one entry and return it ("" when there is none)."""
        if not self.can_go_forward():
            return ""
        self._index += 1
        return self._entries[self._index]

    def go(self, index: int) -> str:
        """Jump to an absolute position in the stack (for a drop-down menu)."""
        if not 0 <= index < len(self._entries):
            return ""
        self._index = index
        return self._entries[self._index]

    def clear(self) -> None:
        self._entries.clear()
        self._index = -1


def mirror_path(source_base: str, source_path: str, target_base: str, *, posix: bool) -> str:
    """Translate ``source_path`` under ``source_base`` into ``target_base``.

    This is what "mirror navigation" needs: the user walks into
    ``/var/www/site/public`` on one side and the other side should follow to
    ``…/site/public`` beneath its own root. Returns "" when ``source_path`` is
    not inside ``source_base``, which is the caller's cue to leave the other
    pane alone.
    """
    source_rel = _relative(source_base, source_path)
    if source_rel is None:
        return ""
    if not source_rel:
        return target_base
    if posix:
        return posixpath.normpath(posixpath.join(target_base or "/", source_rel))
    import os  # local: keeps the POSIX path of this module import-light

    return os.path.normpath(os.path.join(target_base, source_rel.replace("/", os.sep)))


def _relative(base: str, path: str) -> str | None:
    """POSIX-ish relative path, tolerating both separators. None if outside."""
    base_parts = _split(base)
    path_parts = _split(path)
    if len(path_parts) < len(base_parts):
        return None
    for left, right in zip(base_parts, path_parts):
        if left.lower() != right.lower():  # Windows paths are case-insensitive
            return None
    return "/".join(path_parts[len(base_parts):])


def _split(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
