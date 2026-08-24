"""The saved snippet library: parameterised commands you run on a server.

A snippet is a one-line (or several-line) shell command with placeholders that
are filled in from the tab's current state, so ``composer install`` runs in the
directory you are looking at rather than wherever the shell happened to start.

Placeholders are written ``{name}`` and are substituted *quoted*, so a path
with a space in it cannot split into two arguments, and a value cannot inject
another command. Unknown placeholders are left alone rather than emptied, which
makes a typo visible instead of silently destructive.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid
from dataclasses import asdict, dataclass, field

from mysql_runner.paths import app_data_dir

#: Placeholders offered by the file-manager tab.
PLACEHOLDERS = (
    ("remote_dir", "The remote directory currently shown"),
    ("local_dir", "The local directory currently shown"),
    ("file", "The selected remote file or folder name"),
    ("path", "The full remote path of the selection"),
    ("host", "The server host name"),
    ("user", "The connection's user name"),
)

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass
class Snippet:
    """One saved command."""

    name: str
    command: str
    description: str = ""
    #: Ask before running - for anything that restarts or deletes.
    confirm: bool = False
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def placeholders(self) -> list[str]:
        return sorted(set(_PLACEHOLDER.findall(self.command)))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Snippet":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            name=data.get("name", "(unnamed)"),
            command=data.get("command", ""),
            description=data.get("description", ""),
            confirm=bool(data.get("confirm", False)),
            tags=[str(tag) for tag in data.get("tags", []) if str(tag)],
        )


#: Shipped with the app; a first run gets something useful immediately.
DEFAULT_SNIPPETS = (
    Snippet(
        name="Disk free",
        command="df -h",
        description="Free space per mount",
        tags=["status"],
    ),
    Snippet(
        name="Largest files here",
        command="du -ah {remote_dir} | sort -rh | head -n 20",
        description="The twenty biggest things in the current directory",
        tags=["status"],
    ),
    Snippet(
        name="Fix web permissions",
        command=(
            "find {remote_dir} -type d -exec chmod 755 {} + && "
            "find {remote_dir} -type f -exec chmod 644 {} +"
        ),
        description="755 on folders, 644 on files, below the current directory",
        confirm=True,
        tags=["permissions"],
    ),
    Snippet(
        name="Composer install",
        command="cd {remote_dir} && composer install --no-dev --optimize-autoloader",
        description="Install PHP dependencies for production",
        confirm=True,
        tags=["deploy", "php"],
    ),
    Snippet(
        name="Laravel: clear caches",
        command=(
            "cd {remote_dir} && php artisan cache:clear && php artisan config:clear "
            "&& php artisan view:clear"
        ),
        description="Clear the three caches that break a deploy",
        confirm=True,
        tags=["deploy", "php"],
    ),
    Snippet(
        name="Git pull",
        command="cd {remote_dir} && git pull --ff-only",
        description="Fast-forward the checkout in this directory",
        confirm=True,
        tags=["deploy", "git"],
    ),
    Snippet(
        name="Restart nginx",
        command="sudo systemctl restart nginx",
        description="Needs a sudo rule that does not prompt",
        confirm=True,
        tags=["service"],
    ),
    Snippet(
        name="PHP-FPM reload",
        command="sudo systemctl reload php-fpm || sudo systemctl reload php8.2-fpm",
        description="Reload FPM under either service name",
        confirm=True,
        tags=["service"],
    ),
    Snippet(
        name="Tail the error log",
        command="tail -n 100 {remote_dir}/error_log",
        description="Last hundred lines of the log beside your files",
        tags=["logs"],
    ),
)


class SnippetLibrary:
    """Snippets on disk, in plain JSON beside the settings."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = str(path) if path else str(app_data_dir() / "snippets.json")
        self._snippets: list[Snippet] = []
        self._loaded = False

    @property
    def path(self) -> str:
        return self._path

    # ----- persistence ----------------------------------------------------
    def load(self) -> list[Snippet]:
        if self._loaded:
            return self._snippets
        self._loaded = True
        if not os.path.isfile(self._path):
            self._snippets = [
                Snippet.from_dict(item.to_dict()) for item in DEFAULT_SNIPPETS
            ]
            return self._snippets
        try:
            raw = json.loads(open(self._path, encoding="utf-8").read())
        except (OSError, ValueError):
            self._snippets = []
            return self._snippets
        items = raw.get("snippets", raw) if isinstance(raw, dict) else raw
        self._snippets = [
            Snippet.from_dict(item) for item in items if isinstance(item, dict)
        ]
        return self._snippets

    def save(self) -> None:
        payload = json.dumps(
            {"version": 1, "snippets": [item.to_dict() for item in self._snippets]},
            indent=2,
        )
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        temp = self._path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp, self._path)

    # ----- editing --------------------------------------------------------
    def all(self) -> list[Snippet]:
        return list(self.load())

    def tags(self) -> list[str]:
        found: set[str] = set()
        for snippet in self.load():
            found.update(snippet.tags)
        return sorted(found)

    def get(self, snippet_id: str) -> Snippet | None:
        return next((item for item in self.load() if item.id == snippet_id), None)

    def add(self, snippet: Snippet) -> Snippet:
        self.load().append(snippet)
        self.save()
        return snippet

    def update(self, snippet: Snippet) -> bool:
        items = self.load()
        for index, existing in enumerate(items):
            if existing.id == snippet.id:
                items[index] = snippet
                self.save()
                return True
        return False

    def delete(self, snippet_id: str) -> bool:
        items = self.load()
        remaining = [item for item in items if item.id != snippet_id]
        if len(remaining) == len(items):
            return False
        self._snippets = remaining
        self.save()
        return True

    def restore_defaults(self) -> int:
        """Add back any shipped snippet that has been deleted."""
        existing = {item.name for item in self.load()}
        added = 0
        for shipped in DEFAULT_SNIPPETS:
            if shipped.name not in existing:
                self._snippets.append(Snippet.from_dict(shipped.to_dict()))
                added += 1
        if added:
            self.save()
        return added

    def search(self, text: str) -> list[Snippet]:
        needle = text.strip().lower()
        if not needle:
            return self.all()
        return [
            item
            for item in self.load()
            if needle in item.name.lower()
            or needle in item.command.lower()
            or needle in item.description.lower()
            or any(needle in tag.lower() for tag in item.tags)
        ]


def render(command: str, context: dict[str, str]) -> str:
    """Fill in ``{placeholders}`` with shell-quoted values."""

    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in context:
            return match.group(0)  # Leave a typo visible rather than blank.
        value = context[key]
        return shlex.quote(value) if value else "''"

    return _PLACEHOLDER.sub(substitute, command)


def missing_placeholders(command: str, context: dict[str, str]) -> list[str]:
    """Placeholders the current context cannot fill."""
    return sorted(
        {name for name in _PLACEHOLDER.findall(command) if not context.get(name)}
    )
