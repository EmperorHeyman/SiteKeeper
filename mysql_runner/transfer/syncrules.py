"""Folder sync rules: which local folders keep themselves on the server.

A rule pairs one local folder with one remote folder and says *when* the pair
should be reconciled:

* ``off``       - remembered, but nothing happens by itself,
* ``on_save``   - the moment a file under the folder settles on disk,
* ``on_commit`` - the moment the local git repository records a commit.

Rules are plain data - two paths, a mode and two flags, no credentials - so they
live in an unencrypted JSON file beside ``settings.json``, keyed by the profile
they belong to. They deliberately survive restarts: a folder marked as synced
last week should still be syncing today without being armed again by hand.

Nothing here imports Qt, so the rules can be read and written by the tests and
by the other front end as well as by the desktop app.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from mysql_runner.paths import sync_rules_path


class SyncMode(str, Enum):
    """When a synced folder reconciles itself."""

    OFF = "off"
    ON_SAVE = "on_save"
    ON_COMMIT = "on_commit"

    @property
    def label(self) -> str:
        return {
            SyncMode.OFF: "Paused",
            SyncMode.ON_SAVE: "On save",
            SyncMode.ON_COMMIT: "On git commit",
        }[self]

    @property
    def is_live(self) -> bool:
        """Whether this mode needs a watcher running."""
        return self is not SyncMode.OFF


def normalise_local(path: str) -> str:
    """One spelling per local folder, so rules can be matched by equality."""
    if not path:
        return ""
    return os.path.normpath(os.path.abspath(path)).rstrip("\\/") or path


def normalise_remote(path: str) -> str:
    """One spelling per remote folder. A bare slash is left alone; it is real."""
    if not path:
        return ""
    stripped = path.rstrip("/")
    return stripped or "/"


def _key(path: str) -> str:
    """Comparison key for a local path (Windows paths ignore letter case)."""
    return os.path.normcase(normalise_local(path))


@dataclass
class SyncRule:
    """One local folder kept on the server, and the trigger that does it."""

    profile_id: str
    local: str
    remote: str
    mode: SyncMode = SyncMode.ON_SAVE
    #: Remove the copy on the server when the local file goes away. Chosen
    #: deliberately; a single rule can still turn it off on its own.
    delete_remote: bool = True
    #: Apply .deployignore / .gitignore to the transfers this rule starts.
    use_ignore_rules: bool = True
    #: Include everything below the folder. False covers the files sitting in
    #: the folder itself and nothing else - the only sane way to sync a site
    #: root, whose subdirectories are either huge or synced in their own right.
    recursive: bool = True
    #: Remove server-only files found by a full sync without asking first. A
    #: full sync cannot tell "you deleted this" from "the server made this", so
    #: it asks once per folder unless the answer has been remembered here.
    auto_remove: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self.local = normalise_local(self.local)
        self.remote = normalise_remote(self.remote)
        if not isinstance(self.mode, SyncMode):
            self.mode = SyncMode(str(self.mode))

    # ----- path arithmetic ------------------------------------------------
    def covers(self, local_path: str) -> bool:
        """Whether ``local_path`` is this folder or something inside it."""
        if not self.local:
            return False
        mine = _key(self.local)
        theirs = _key(local_path)
        return theirs == mine or theirs.startswith(mine + os.sep)

    def relative(self, local_path: str) -> str:
        """``local_path`` as a forward-slash path relative to the folder."""
        rel = os.path.relpath(normalise_local(local_path), self.local)
        if rel in (".", ""):
            return ""
        return rel.replace("\\", "/")

    def remote_for(self, local_path: str) -> str:
        """Where a file inside the folder belongs on the server."""
        rel = self.relative(local_path)
        if not rel or rel.startswith(".."):
            return self.remote
        base = "" if self.remote == "/" else self.remote
        return f"{base}/{rel}"

    # ----- presentation ---------------------------------------------------
    @property
    def name(self) -> str:
        return os.path.basename(self.local) or self.local

    @property
    def scope(self) -> str:
        return "with subfolders" if self.recursive else "files in it only"

    def owns(self, local_path: str) -> bool:
        """Whether this rule is responsible for one particular file.

        Same as :meth:`covers` for a recursive rule; a non-recursive one owns
        only what sits directly in its folder.
        """
        if not self.covers(local_path):
            return False
        if self.recursive:
            return True
        return "/" not in self.relative(local_path)

    def describe(self) -> str:
        return f"{self.local} -> {self.remote} ({self.mode.label}, {self.scope})"

    # ----- serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "local": self.local,
            "remote": self.remote,
            "mode": self.mode.value,
            "delete_remote": self.delete_remote,
            "use_ignore_rules": self.use_ignore_rules,
            "recursive": self.recursive,
            "auto_remove": self.auto_remove,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SyncRule":
        try:
            mode = SyncMode(str(data.get("mode", SyncMode.ON_SAVE.value)))
        except ValueError:
            mode = SyncMode.ON_SAVE
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            profile_id=str(data.get("profile_id", "")),
            local=str(data.get("local", "")),
            remote=str(data.get("remote", "")),
            mode=mode,
            delete_remote=bool(data.get("delete_remote", True)),
            use_ignore_rules=bool(data.get("use_ignore_rules", True)),
            recursive=bool(data.get("recursive", True)),
            auto_remove=bool(data.get("auto_remove", False)),
        )


class SyncRuleStore:
    """Every sync rule on this machine, grouped by the profile that owns it."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or sync_rules_path()
        self._rules: list[SyncRule] = []
        self.load()

    # ----- persistence ---------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._rules = []
            return
        items = raw.get("rules", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            self._rules = []
            return
        rules: list[SyncRule] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rule = SyncRule.from_dict(item)
            if rule.local and rule.remote and rule.profile_id:
                rules.append(rule)
        self._rules = rules

    def save(self) -> None:
        payload = {"version": 1, "rules": [rule.to_dict() for rule in self._rules]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # ----- reading -------------------------------------------------------
    def all(self) -> list[SyncRule]:
        return list(self._rules)

    def for_profile(self, profile_id: str) -> list[SyncRule]:
        return [rule for rule in self._rules if rule.profile_id == profile_id]

    def get(self, rule_id: str) -> SyncRule | None:
        return next((rule for rule in self._rules if rule.id == rule_id), None)

    def find(self, profile_id: str, local: str) -> SyncRule | None:
        """The rule for exactly this folder, if there is one."""
        wanted = _key(local)
        return next(
            (
                rule
                for rule in self._rules
                if rule.profile_id == profile_id and _key(rule.local) == wanted
            ),
            None,
        )

    def owner(self, profile_id: str, local: str) -> SyncRule | None:
        """The rule responsible for one path: the deepest one that owns it.

        Unlike :meth:`covering` this respects a rule's scope, so a file in a
        subfolder belongs to the enclosing recursive rule rather than to a
        files-only rule higher up.
        """
        matches = [
            rule
            for rule in self._rules
            if rule.profile_id == profile_id and rule.owns(local)
        ]
        if not matches:
            return None
        return max(matches, key=lambda rule: len(_key(rule.local)))

    def covering(self, profile_id: str, local: str) -> SyncRule | None:
        """The closest rule whose folder contains ``local`` (deepest wins)."""
        matches = [
            rule
            for rule in self._rules
            if rule.profile_id == profile_id and rule.covers(local)
        ]
        if not matches:
            return None
        return max(matches, key=lambda rule: len(_key(rule.local)))

    # ----- writing -------------------------------------------------------
    def put(self, rule: SyncRule) -> SyncRule:
        """Add ``rule``, or replace the one already covering the same folder."""
        existing = self.find(rule.profile_id, rule.local)
        if existing is not None:
            rule.id = existing.id
            self._rules = [rule if r.id == existing.id else r for r in self._rules]
        else:
            self._rules.append(rule)
        self.save()
        return rule

    def set_flag(self, rule_id: str, name: str, value: bool) -> SyncRule | None:
        """Flip one of a rule's booleans and write the file."""
        rule = self.get(rule_id)
        if rule is None or not hasattr(rule, name):
            return None
        setattr(rule, name, bool(value))
        self.save()
        return rule

    def set_mode(self, rule_id: str, mode: SyncMode) -> SyncRule | None:
        rule = self.get(rule_id)
        if rule is None:
            return None
        rule.mode = mode
        self.save()
        return rule

    def remove(self, rule_id: str) -> bool:
        before = len(self._rules)
        self._rules = [rule for rule in self._rules if rule.id != rule_id]
        if len(self._rules) == before:
            return False
        self.save()
        return True

    def remove_local(self, profile_id: str, local: str) -> bool:
        rule = self.find(profile_id, local)
        return self.remove(rule.id) if rule is not None else False

    def clear_profile(self, profile_id: str) -> int:
        keep = [rule for rule in self._rules if rule.profile_id != profile_id]
        removed = len(self._rules) - len(keep)
        if removed:
            self._rules = keep
            self.save()
        return removed
