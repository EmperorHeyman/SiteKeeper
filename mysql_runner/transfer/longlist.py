"""Parsing the one listing format a Unix shell and an FTP server both speak.

``drwxr-xr-x 2 www-data www-data 4096 Jan 14 09:31 uploads``

Two quite different callers need this. An FTP server that will not speak MLSD
answers LIST with these lines, and there is nothing else to read. An SFTP
session has structured attributes and needs no parsing at all - except that
they carry *numeric* uid and gid, and "uid 33" is not an answer to "why can
PHP not write here". ``ls -lA`` over the same SSH connection says ``www-data``,
which is.

So the parser lives here rather than inside either backend, and the mode,
owner and group it finds are what the file panes and the MCP listings show.

Deliberately tolerant: anything that is not an entry - the ``total 8`` header,
a blank line, a warning - comes back as None rather than raising, because a
listing is a report about a directory and one unreadable line in it is not a
reason to fail the whole thing. Timestamps are skipped rather than guessed at:
``ls`` prints a year for old files and a time for recent ones, in the server's
locale, and a wrong date is worse than none.
"""

from __future__ import annotations

import re

from mysql_runner.transfer.base import RemoteEntry

#: Types worth listing. Anything else on a line - a socket, a device - is not
#: something this application can act on.
_TYPES = "-dl"

#: The permission column, checked position by position rather than by its
#: first letter alone. This is not pedantry: ``ls: cannot access '/x/y': No
#: such file or directory`` splits into nine fields and begins with an "l",
#: so a first-letter check reads an error message as a symlink called
#: "directory" owned by "access". The trailing character is the ACL or
#: extended-attribute marker some systems append (``+``, ``.``, ``@``).
_MODE_COLUMN = re.compile(r"^[-dlbcps][-r][-w][-xsS][-r][-w][-xsS][-r][-w][-xtT][+.@]?$")


def parse(text: str) -> list[RemoteEntry]:
    """Every entry in a ``ls -l`` style listing."""
    found = []
    for line in text.splitlines():
        entry = parse_line(line)
        if entry is not None:
            found.append(entry)
    return found


def parse_line(line: str) -> RemoteEntry | None:
    """One listing line, or None when it is not an entry."""
    parts = line.split(maxsplit=8)
    if len(parts) < 9 or not parts[0]:
        return None
    permissions = parts[0]
    if not _MODE_COLUMN.match(permissions) or permissions[0] not in _TYPES:
        return None
    name = parts[8]
    is_link = permissions[0] == "l"
    target = ""
    if is_link and " -> " in name:
        name, target = name.split(" -> ", 1)
    return RemoteEntry(
        name=name,
        is_dir=permissions[0] == "d",
        size=_as_int(parts[4]),
        modified=None,  # see the module docstring
        is_link=is_link,
        mode=mode_from_permissions(permissions),
        link_target=target,
        owner=parts[2],
        group=parts[3],
    )


def mode_from_permissions(text: str) -> int | None:
    """Turn ``rwxr-xr-x`` from a listing line into permission bits.

    ``text`` is the whole first column, type letter included; an ACL or
    SELinux marker after it (``drwxr-xr-x+``, ``-rw-r--r--.``) is ignored,
    which is the only reason this reads a slice rather than the string.
    """
    body = text[1:10]
    if len(body) != 9:
        return None
    mode = 0
    for index, char in enumerate(body):
        if char == "-":
            continue
        bit = (0b100, 0b010, 0b001)[index % 3]
        mode |= bit << (6 - 3 * (index // 3))
    return mode


def mode_letters(mode: int | None) -> str:
    """``rwxr-xr-x``, or "" - the nine characters, without the type letter."""
    if mode is None:
        return ""
    bits = ""
    for group in range(3):
        for index, char in enumerate("rwx"):
            bit = (0b100, 0b010, 0b001)[index]
            bits += char if mode & (bit << (6 - 3 * group)) else "-"
    return bits


def describe_mode(mode: int | None) -> str:
    """``0755 rwxr-xr-x``, or "" when the server never said.

    Both forms, because they answer different questions: the octal is what
    you type into a chmod, and the letters are what you read to see whether
    the group can write.
    """
    if mode is None:
        return ""
    return f"{mode & 0o7777:04o} {mode_letters(mode)}"


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
