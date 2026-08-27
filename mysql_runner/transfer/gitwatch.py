"""Notice when a local git repository records a commit.

This reads git's own files rather than shelling out: ``HEAD``, the ref it points
at, ``packed-refs`` as a fallback, and ``logs/HEAD`` for the human-readable
detail. That keeps the feature working when git is not on PATH (a packaged build
on a machine with no git install), costs nothing but two small file reads per
poll, and cannot be slowed down by a repository-wide lock the way ``git status``
can.

Any move of HEAD counts as a commit event: a commit, an amend, a merge, a reset
or a checkout of another branch. They all mean "the tree you asked me to deploy
now looks different", which is exactly when a commit-triggered sync should run.

Nothing here imports Qt.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable

#: How often to look at HEAD, in seconds. Two small reads; cheap enough to be
#: frequent, slow enough not to spin on a network drive.
INTERVAL = 2.0


def find_repo(path: str) -> str | None:
    """The work-tree root of the repository containing ``path``, if any."""
    if not path:
        return None
    current = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def git_dir(repo_root: str) -> str | None:
    """The real .git directory for a work tree.

    A linked work tree or a submodule has a ``.git`` *file* holding a
    ``gitdir: <path>`` line instead of a directory, so follow that.
    """
    if not repo_root:
        return None
    candidate = os.path.join(repo_root, ".git")
    if os.path.isdir(candidate):
        return candidate
    try:
        text = _read(candidate)
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(repo_root, target))
            return target if os.path.isdir(target) else None
    return None


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def head_ref(gdir: str) -> str:
    """What HEAD points at: ``refs/heads/main``, or "" when it is detached."""
    try:
        text = _read(os.path.join(gdir, "HEAD")).strip()
    except OSError:
        return ""
    if text.startswith("ref:"):
        return text.split(":", 1)[1].strip()
    return ""


def head_commit(gdir: str) -> str:
    """The commit HEAD resolves to, or "" when the repository has none yet."""
    ref = head_ref(gdir)
    if not ref:
        try:
            text = _read(os.path.join(gdir, "HEAD")).strip()
        except OSError:
            return ""
        return text if _looks_like_sha(text) else ""
    loose = os.path.join(gdir, ref.replace("/", os.sep))
    try:
        text = _read(loose).strip()
    except OSError:
        return packed_ref(gdir, ref)
    return text if _looks_like_sha(text) else ""


def packed_ref(gdir: str, ref: str) -> str:
    """Resolve ``ref`` out of packed-refs, where git puts refs it has packed."""
    try:
        text = _read(os.path.join(gdir, "packed-refs"))
    except OSError:
        return ""
    for line in text.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip()
    return ""


def _looks_like_sha(text: str) -> bool:
    return len(text) >= 7 and all(char in "0123456789abcdef" for char in text.lower())


def commit_changes(
    repo_root: str, old: str, new: str
) -> list[tuple[str, str]] | None:
    """What one move of HEAD changed: (status, repo-relative path) pairs.

    Watching never shells out, but *diffing* two commits has no sane
    re-implementation, so this asks git itself - quietly, and with None as
    the answer whenever git is missing, either commit is unknown, or the
    command fails. Callers treat None as "compare everything instead".

    Statuses are git's single letters: A(dded), M(odified), D(eleted),
    T(ypechange). Renames are disabled so a rename arrives as its D and A.
    """
    if not repo_root or not old or not new:
        return None
    if not (_looks_like_sha(old) and _looks_like_sha(new)):
        return None
    import subprocess

    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [
                "git", "-C", repo_root,
                "diff", "--name-status", "--no-renames", "-z", old, new,
            ],
            capture_output=True,
            timeout=30,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    tokens = result.stdout.decode("utf-8", errors="replace").split("\0")
    changes: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(tokens):
        status, path = tokens[index].strip(), tokens[index + 1]
        index += 2
        if status and path:
            changes.append((status[0].upper(), path))
    return changes


def reflog_detail(gdir: str, commit: str) -> str:
    """What the reflog says about the move to ``commit`` ("commit: fix login").

    The reflog is a plain text file and is on by default in every non-bare
    repository, but a repository with it disabled is not an error - the sync
    just runs without a subject line to show.
    """
    try:
        text = _read(os.path.join(gdir, "logs", "HEAD"))
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if "\t" not in line:
            continue
        heads, message = line.split("\t", 1)
        parts = heads.split()
        if len(parts) >= 2 and parts[1].lower().startswith(commit.lower()[:40]):
            return message.strip()
    return ""


@dataclass(frozen=True)
class CommitEvent:
    """One observed move of HEAD."""

    old: str
    new: str
    ref: str = ""
    detail: str = ""

    @property
    def branch(self) -> str:
        return self.ref.rsplit("/", 1)[-1] if self.ref else "detached HEAD"

    @property
    def short(self) -> str:
        return self.new[:8]

    def describe(self) -> str:
        text = f"{self.branch} at {self.short}"
        return f"{text} ({self.detail})" if self.detail else text


class GitCommitWatcher:
    """Watches one work tree for commits, on its own thread."""

    def __init__(
        self,
        root: str,
        on_commit: Callable[[CommitEvent], None],
        *,
        interval: float = INTERVAL,
        on_message: Callable[[str], None] | None = None,
    ) -> None:
        self._repo = find_repo(root) or ""
        self._git_dir = git_dir(self._repo) if self._repo else None
        self._on_commit = on_commit
        self._on_message = on_message
        self._interval = max(0.5, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._head = ""
        self._ref = ""

    # ----- state ----------------------------------------------------------
    @property
    def repo(self) -> str:
        return self._repo

    @property
    def valid(self) -> bool:
        """Whether there is a repository here at all."""
        return bool(self._git_dir)

    @property
    def head(self) -> str:
        return self._head

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ----- lifecycle ------------------------------------------------------
    def start(self, *, prime: bool = True) -> bool:
        """Begin watching. ``prime`` treats the current commit as already seen."""
        if not self.valid or self.running:
            return False
        self._stop.clear()
        if prime:
            self._head = head_commit(self._git_dir or "")
            self._ref = head_ref(self._git_dir or "")
        self._thread = threading.Thread(
            target=self._loop, name="watch-git", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    # ----- the loop -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                event = self.poll()
            except OSError as exc:
                self._message(f"Watching git stopped: {exc}")
                return
            if event is None:
                continue
            try:
                self._on_commit(event)
            except Exception as exc:  # a bad callback must not kill the thread
                self._message(f"Commit handler failed: {exc}")

    def poll(self) -> CommitEvent | None:
        """One pass. Returns an event when HEAD has moved since the last one."""
        gdir = self._git_dir
        if not gdir:
            return None
        commit = head_commit(gdir)
        ref = head_ref(gdir)
        if not commit or commit == self._head:
            self._ref = ref or self._ref
            return None
        old, self._head = self._head, commit
        self._ref = ref
        return CommitEvent(
            old=old,
            new=commit,
            ref=ref,
            detail=reflog_detail(gdir, commit),
        )

    def _message(self, text: str) -> None:
        if self._on_message is not None:
            self._on_message(text)


def describe_repo(path: str) -> str:
    """A short "repo (branch)" label for a folder, or "" when it is not one."""
    root = find_repo(path)
    if not root:
        return ""
    gdir = git_dir(root)
    if not gdir:
        return ""
    ref = head_ref(gdir)
    branch = ref.rsplit("/", 1)[-1] if ref else "detached HEAD"
    return f"{os.path.basename(root) or root} ({branch})"
