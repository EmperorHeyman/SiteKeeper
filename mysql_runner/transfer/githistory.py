"""Reading a repository's past: what was committed, and what a file looked like.

``gitwatch.py`` watches HEAD by reading git's own files, because noticing a
commit has to work on a machine with no git installed. Reading *history* is a
different job: resolving a tree, walking parents and extracting a blob have no
sane re-implementation, so everything here shells out to git and answers None
when it cannot - the caller says "git is not available here" rather than
showing half a log.

The point of it all is the last function: a file as it was at some commit,
written to a scratch path so it can be uploaded like any other file. That is
what makes "put yesterday's version back on the server" a thing this app can
do without touching the working tree - no checkout, no stash, no detached HEAD,
nothing for the user to undo afterwards.

Nothing here imports Qt.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

#: Windows: never flash a console window for a background git call.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: How many commits a log asks for unless told otherwise. Enough to cover
#: "which one was it?" without paying for a decade of history.
DEFAULT_LIMIT = 200

#: Record and field separators for --pretty. Chosen because they cannot occur
#: in a commit subject, unlike anything printable.
_RECORD = "\x1e"
_FIELD = "\x1f"

#: Refuse to extract a blob bigger than this into the scratch directory; a
#: repository can hold a 2 GB binary and the answer must not be a hung app.
MAX_BLOB_BYTES = 256 * 1024 * 1024


class GitUnavailable(Exception):
    """git could not be run, or refused the repository."""


def _run(repo: str, args: list[str], *, timeout: float = 30.0) -> bytes | None:
    """One git command in ``repo``. None whenever git cannot answer."""
    if not repo:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def available() -> bool:
    """Whether a git executable can be run at all."""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0


@dataclass(frozen=True)
class Commit:
    """One entry of the log, in the shape the history window shows it."""

    sha: str
    author: str = ""
    when: float = 0.0
    subject: str = ""
    refs: str = ""

    @property
    def short(self) -> str:
        return self.sha[:8]

    @property
    def date(self) -> str:
        if not self.when:
            return ""
        return datetime.fromtimestamp(self.when).strftime("%Y-%m-%d %H:%M")

    def describe(self) -> str:
        text = f"{self.short} {self.subject}".strip()
        return f"{text} ({self.date})" if self.date else text


def commit_log(
    repo: str, *, limit: int = DEFAULT_LIMIT, path: str = "", skip: int = 0
) -> list[Commit] | None:
    """The most recent commits, newest first. None when git cannot answer.

    ``path`` limits the log to commits touching one file or folder, which is
    how "when did this page last change?" is asked.
    """
    fields = ("%H", "%an", "%at", "%s", "%D")
    args = [
        "log",
        f"--max-count={max(1, int(limit))}",
        f"--skip={max(0, int(skip))}",
        f"--pretty=format:{_FIELD.join(fields)}{_RECORD}",
    ]
    if path:
        args += ["--", path]
    out = _run(repo, args, timeout=60.0)
    if out is None:
        return None
    commits: list[Commit] = []
    for record in out.decode("utf-8", errors="replace").split(_RECORD):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD)
        if len(parts) < 4:
            continue
        try:
            when = float(parts[2])
        except ValueError:
            when = 0.0
        commits.append(
            Commit(
                sha=parts[0].strip(),
                author=parts[1].strip(),
                when=when,
                subject=parts[3].strip(),
                refs=parts[4].strip() if len(parts) > 4 else "",
            )
        )
    return commits


def commit_subject(repo: str, sha: str) -> str:
    """The one-line message of one commit, or "" when git cannot say.

    Cheap enough to ask for on every commit a watched folder sees: one
    ``git log -1``, no diff and no tree walk. "" is not an error - a headline
    without the subject is the behaviour there was before it could be had.
    """
    if not sha:
        return ""
    out = _run(repo, ["log", "-1", "--format=%s", sha], timeout=15.0)
    if not out:
        return ""
    lines = out.decode("utf-8", errors="replace").splitlines()
    return lines[0].strip() if lines else ""


def commit_files(repo: str, sha: str) -> list[tuple[str, str]] | None:
    """(status, path) for everything one commit changed.

    A merge commit has no single "what changed", so git prints nothing for it
    by default; ``-m --first-parent`` asks for the diff against the branch it
    was merged into, which is the one anybody means.
    """
    if not sha:
        return None
    out = _run(
        repo,
        [
            "show", "--name-status", "--no-renames", "--format=", "-z",
            "-m", "--first-parent", sha,
        ],
        timeout=60.0,
    )
    if out is None:
        return None
    return _parse_name_status(out)


def _parse_name_status(out: bytes) -> list[tuple[str, str]]:
    tokens = out.decode("utf-8", errors="replace").split("\0")
    changes: list[tuple[str, str]] = []
    seen: set[str] = set()
    index = 0
    while index + 1 < len(tokens):
        status, path = tokens[index].strip(), tokens[index + 1]
        index += 2
        if not status or not path or path in seen:
            continue
        seen.add(path)
        changes.append((status[0].upper(), path))
    return changes


def tree_files(repo: str, sha: str, *, subdir: str = "") -> list[str] | None:
    """Every file the repository held at ``sha``, as repository-relative paths.

    ``subdir`` (repository-relative, forward slashes) narrows it to one folder,
    which keeps "publish the whole site as of last Tuesday" from listing the
    build scripts alongside it.
    """
    if not sha:
        return None
    args = ["ls-tree", "-r", "--name-only", "-z", sha]
    if subdir:
        args += ["--", subdir.rstrip("/") + "/"]
    out = _run(repo, args, timeout=60.0)
    if out is None:
        return None
    return [
        name
        for name in out.decode("utf-8", errors="replace").split("\0")
        if name
    ]


def file_at(repo: str, sha: str, rel: str) -> bytes | None:
    """One file's bytes as of ``sha``. None when it did not exist then."""
    if not sha or not rel:
        return None
    return _run(repo, ["show", f"{sha}:{rel}"], timeout=120.0)


def blob_size(repo: str, sha: str, rel: str) -> int:
    """How big that file was, without reading it. -1 when it is unknown."""
    out = _run(repo, ["cat-file", "-s", f"{sha}:{rel}"], timeout=30.0)
    if out is None:
        return -1
    try:
        return int(out.decode("ascii", errors="replace").strip())
    except ValueError:
        return -1


@dataclass
class Export:
    """The result of writing some old version of a tree to a scratch folder."""

    root: str
    #: (scratch path, repository-relative path) for each file written.
    files: list[tuple[str, str]]
    #: (repository-relative path, why) for each file that could not be.
    failures: list[tuple[str, str]]

    @property
    def ok(self) -> bool:
        return bool(self.files)


def export_files(
    repo: str,
    sha: str,
    rels: list[str],
    dest_root: str,
    *,
    max_bytes: int = MAX_BLOB_BYTES,
    on_progress=None,
) -> Export:
    """Write ``rels`` as they were at ``sha`` into ``dest_root``.

    The layout under ``dest_root`` mirrors the repository, so the caller can
    upload the lot with the same "flatten against this root" logic every other
    batch transfer uses. The working tree is never touched.
    """
    files: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    for index, rel in enumerate(rels, start=1):
        if on_progress is not None:
            on_progress(rel, index, len(rels))
        size = blob_size(repo, sha, rel)
        if size > max_bytes:
            failures.append((rel, f"{size} bytes is too big to publish this way"))
            continue
        data = file_at(repo, sha, rel)
        if data is None:
            failures.append((rel, "not in this commit"))
            continue
        target = os.path.join(dest_root, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            failures.append((rel, str(exc)))
            continue
        files.append((target, rel))
    return Export(root=dest_root, files=files, failures=failures)
