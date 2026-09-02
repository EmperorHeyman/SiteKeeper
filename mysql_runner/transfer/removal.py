"""Deleting on a server, at a speed that matches how much there is.

Removing a tree over SFTP the obvious way is one round trip per file plus one
per directory, done strictly in order because a directory cannot go until it
is empty. On a link with any real latency that is where the time goes, exactly
as it does for a deploy: a `node_modules` of twelve thousand files at 40 ms is
minutes of waiting during which nothing else on that connection can happen.

Where the account has a shell - which is where the app already runs archives,
grep and disk usage - `rm -rf` does the whole tree in *one* round trip, and the
server does the walking. Everywhere else (FTP, FTPS, SFTP-only accounts) the
walk still happens here, iteratively rather than recursively so a pathological
tree cannot exhaust Python's stack, and reporting the error that actually
happened instead of the one a blind `rmdir` would produce afterwards.

Qt-free, so the file manager, the MCP server and the FastAPI backend can all
delete the same way.
"""

from __future__ import annotations

from typing import Callable

from mysql_runner.transfer.base import Capability, RemoteFS, TransferError

#: Returned instead of a count when the server removed the tree in one command
#: and did not say how much that was.
UNCOUNTED = -1

#: How long `rm -rf` is given. A tree big enough to need this is exactly the
#: tree worth handing to the server in the first place.
SHELL_TIMEOUT = 600.0


def guard(path: str) -> str:
    """Return ``path`` cleaned, or refuse the ones nobody means to delete.

    Worth being strict about now that a shell may be involved: a bad path in a
    per-entry walk fails on the first `rmdir`, and the same bad path in an
    `rm -rf` does not fail at all.
    """
    cleaned = (path or "").strip()
    if not cleaned or cleaned.rstrip("/") in ("", ".", "..", "~"):
        raise TransferError(
            "Refusing to delete that: name a file or a folder, not the root."
        )
    return cleaned


def delete_tree(fs: RemoteFS, path: str, *, allow_shell: bool = True) -> int:
    """Remove a directory and everything below it.

    Returns how many entries went, or :data:`UNCOUNTED` when the server did it
    in one command - counting them would have meant walking the tree first,
    which is the cost the command exists to avoid.
    """
    path = guard(path)
    if allow_shell and fs.supports(Capability.EXEC):
        removed = _shell_delete(fs, path)
        if removed is not None:
            return removed
    if _is_symlink(fs, path):
        # A link to a directory lists as that directory, so walking it would
        # empty somebody else's folder and leave the link behind pointing at
        # the hole. `rm -rf` gets this right for free; the walk has to ask.
        fs.remove(path)
        return 1
    return _walk_delete(fs, path)


def _is_symlink(fs: RemoteFS, path: str) -> bool:
    """Whether ``path`` is a symlink. False wherever links do not exist."""
    if not fs.supports(Capability.SYMLINK):
        return False
    try:
        return bool(fs.readlink(path))
    except TransferError:
        return False


def delete_paths(
    fs: RemoteFS,
    paths: list,
    *,
    allow_shell: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[int, list[str]]:
    """Remove several paths, files or trees. Returns (removed, failures).

    Each entry is either a path or a ``(path, is_dir)`` pair. Pass the pair
    wherever the caller already knows which it is - a file manager always
    does, it is in the listing on screen - and that is a round trip per entry
    saved; a bare path is stat'ed here instead.

    One failure does not stop the rest: a selection of thirty where one file
    is read-only should lose the other twenty-nine, and name the one it kept.
    """
    removed = 0
    failures: list[str] = []
    total = len(paths)
    for index, entry in enumerate(paths, start=1):
        path, is_dir = _split_entry(entry)
        name = fs.basename(path)
        if on_progress is not None:
            on_progress(index, total, name)
        if is_dir is None:
            try:
                stat = fs.stat(path)
            except TransferError:
                continue  # already gone: nothing to do and nothing to report
            is_dir = stat.is_dir and not stat.is_link
        try:
            if is_dir:
                delete_tree(fs, path, allow_shell=allow_shell)
            else:
                fs.remove(guard(path))
        except TransferError as exc:
            if _already_gone(fs, path):
                continue  # somebody else got there first; nothing to report
            failures.append(f"{name}: {exc}")
            continue
        removed += 1
    return removed, failures


def _already_gone(fs: RemoteFS, path: str) -> bool:
    """Whether a removal failed because there was nothing there.

    Only asked after a failure, so the round trip is paid on the rare path and
    never on the ordinary one. A file that vanished between the listing on
    screen and the click - a deploy replaced it, a cron job tidied it - is not
    a delete that went wrong, and saying so would be noise on a selection of
    thirty where twenty-nine went perfectly.
    """
    try:
        fs.stat(path)
    except TransferError:
        return True
    return False


def _split_entry(entry: object) -> tuple[str, bool | None]:
    """Accept "path" or ("path", is_dir). None means "ask the server"."""
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return str(entry[0]), bool(entry[1])
    return str(entry), None


def _shell_delete(fs: RemoteFS, path: str) -> int | None:
    """One `rm -rf`. None means the command could not be run at all."""
    from mysql_runner.transfer.remote_exec import quote, run

    try:
        result = run(fs, f"rm -rf -- {quote(path)}", timeout=SHELL_TIMEOUT)
    except TransferError:
        return None  # no shell after all, or the session refused it
    if result.ok:
        return UNCOUNTED
    # The command ran and said no. That is the server's own answer - a
    # permission problem, a read-only mount - and walking the tree by hand
    # would spend a thousand round trips arriving at the same one.
    detail = (result.stderr or result.stdout).strip().splitlines()
    raise TransferError(detail[0] if detail else f"Could not delete {path}.")


def _walk_delete(fs: RemoteFS, path: str) -> int:
    """Post-order removal without recursion.

    Directories go on the stack twice: once to be listed, and once - behind
    everything they contain - to be removed. That is what makes the traversal
    post-order without the call stack a recursive version would need, and a
    site with a deeply nested cache is exactly the tree that used to reach
    Python's recursion limit and fail with a message about recursion rather
    than about files.
    """
    removed = 0
    stack: list[tuple[str, bool]] = [(path, False)]
    while stack:
        current, emptied = stack.pop()
        if emptied:
            fs.rmdir(current)
            removed += 1
            continue
        try:
            entries = fs.listdir(current)
        except TransferError as listing_error:
            # Either this is a file (the caller's stat can be out of date, and
            # symlinks lie), or the directory genuinely cannot be read. Try it
            # as a file to settle the first; if that fails too, the listing's
            # error is the one worth showing - a blind rmdir here used to
            # report "directory not empty", which describes neither case.
            try:
                fs.remove(current)
            except TransferError:
                raise listing_error from None
            removed += 1
            continue
        stack.append((current, True))
        for entry in entries:
            child = fs.join(current, entry.name)
            if entry.is_dir and not entry.is_link:
                stack.append((child, False))
            else:
                fs.remove(child)
                removed += 1
    return removed
