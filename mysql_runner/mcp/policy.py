"""What Claude may do through MCP, decided in the app rather than on a command line.

This used to live in the flags a client config passed to the server process,
and that was wrong in three ways that only showed up in use.

It was invisible. Nothing in Sitekeeper could tell you what Claude was
currently allowed to do, because the answer was in a JSON file belonging to
another program - and Claude Code keeps a *separate* set of those per project,
so the same server was read-only in one folder and could delete in another
with no indication which you were talking to.

It was per-client. Registering the server twice, or from Claude Desktop as
well as Claude Code, meant two lists of flags to keep in step.

And it could only be changed by restarting the server. Every refusal ended in
"restart the MCP server with --allow-x", which means quitting your assistant
mid-task to edit a config file you may not be able to find.

So the grants live here instead: one file next to the vault, owned by the app,
re-read on every tool call. Ticking a box in Sitekeeper takes effect on the
next thing Claude tries, with nothing restarted.

Everything defaults to off. A file that is missing, empty or unreadable is a
read-only policy, never a permissive one - the failure mode of a corrupt
grants file must be that Claude can look but not touch.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field

from mysql_runner.paths import mcp_policy_path


@dataclass
class McpPolicy:
    """The standing answer to "what may Claude do here?".

    Production is granted per connection rather than globally, the same way
    the app's own production guard is disarmed per connection: someone who
    deploys to one live site all day should not have to arm every other live
    site to do it.
    """

    allow_write: bool = False
    allow_delete: bool = False
    allow_sql_write: bool = False
    #: Profile ids MCP may use at all. Empty means every stored profile,
    #: including ones added later - which is the useful default, and the one
    #: the dialog offers first.
    profiles: list[str] = field(default_factory=list)
    #: Profile ids the grants above extend to in spite of a PRODUCTION
    #: marking. A profile named here still needs the matching grant: this
    #: lifts the production block, it does not stand in for allow_write.
    production_profiles: list[str] = field(default_factory=list)

    # ----- reading and writing ------------------------------------------
    @classmethod
    def load(cls) -> "McpPolicy":
        path = mcp_policy_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()  # missing, half-written or corrupt: grant nothing
        if not isinstance(data, dict):
            return cls()
        return cls(
            allow_write=bool(data.get("allow_write", False)),
            allow_delete=bool(data.get("allow_delete", False)),
            allow_sql_write=bool(data.get("allow_sql_write", False)),
            profiles=_id_list(data.get("profiles")),
            production_profiles=_id_list(data.get("production_profiles")),
        )

    def save(self) -> None:
        """Write the file atomically; the MCP process reads it uninvited.

        A plain write is a window in which the server can read nothing (an
        empty file) and fall back to read-only mid-session. Replacing a
        finished temporary file has no such window.
        """
        path = mcp_policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as out:
                json.dump(asdict(self), out, indent=2)
            os.replace(handle.name, path)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    # ----- questions the tools ask --------------------------------------
    def sees(self, profile) -> bool:
        """Whether this profile is in scope at all."""
        if not self.profiles:
            return True
        return profile.id in self.profiles

    def allows_production(self, profile) -> bool:
        """Whether the grants extend to this profile despite its marking."""
        return profile.id in self.production_profiles

    def any_grant(self) -> bool:
        return self.allow_write or self.allow_delete or self.allow_sql_write

    def describe(self) -> str:
        """One line for the server's startup banner on stderr."""
        grants = [
            name
            for name, allowed in (
                ("write", self.allow_write),
                ("delete", self.allow_delete),
                ("sql-write", self.allow_sql_write),
            )
            if allowed
        ]
        scope = f"{len(self.profiles)} profile(s)" if self.profiles else "all profiles"
        prod = (
            f"; production on {len(self.production_profiles)} profile(s)"
            if self.production_profiles
            else ""
        )
        return f"granted: {', '.join(grants) or 'read-only'}; scope: {scope}{prod}"


class LivePolicy:
    """The policy as the tools see it: whatever is on disk right now.

    Re-reading a small JSON file per tool call would be fine on its own, but
    a folder upload asks the same question once per file, so the answer is
    held until the file's timestamp or size moves. That is what makes a tick
    in the app land on the next tool call rather than the next restart.
    """

    def __init__(self) -> None:
        self._cached = McpPolicy.load()
        self._stamp = self._stat()

    @staticmethod
    def _stat() -> tuple[float, int]:
        try:
            info = mcp_policy_path().stat()
        except OSError:
            return (0.0, -1)
        return (info.st_mtime, info.st_size)

    def current(self) -> McpPolicy:
        stamp = self._stat()
        if stamp != self._stamp:
            self._stamp = stamp
            self._cached = McpPolicy.load()
        return self._cached


def _id_list(value: object) -> list[str]:
    """A list of profile ids, ignoring anything that is not one."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]
