"""Split a block of SQL into individual statements.

The console feeds whatever the user typed straight to the server, so it has to
find statement boundaries itself. A naive split on ";" breaks as soon as a
semicolon appears inside a string literal, a quoted identifier, or a comment,
so this walks the text one character at a time and only treats ";" as a
terminator while in normal code.

Two dialects, because the same character means different things in each and
guessing wrong silently mangles a statement:

* **MySQL** quotes identifiers with backticks, treats ``#`` as a comment and a
  backslash as an escape inside string literals, and a trailing ``\\G`` asks
  for vertical output rather than being sent to the server.
* **T-SQL** (SQL Server) quotes identifiers with ``[brackets]``, has no
  backslash escapes, and - the one that bites - uses ``#`` to name a temporary
  table. ``SELECT * FROM #tmp`` read as MySQL loses the rest of the line to a
  comment. It also ends batches with a bare ``GO`` line, which is a client
  instruction like ``\\G`` and never reaches the server either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A line that is nothing but sqlcmd's batch separator, optionally with a
#: repeat count ("GO 5"). Matched against one line at a time.
_GO_LINE = re.compile(r"[ \t]*[gG][oO][ \t]*(\d+)?[ \t]*(?:\r?\n|$)")


@dataclass(frozen=True)
class Statement:
    """One statement ready to execute."""

    sql: str
    #: True when the user ended it with \\G instead of ";". MySQL only.
    vertical: bool = False
    #: How many times to run it - sqlcmd's "GO 5". Always 1 for MySQL.
    repeat: int = 1


def split_statements(text: str, *, tsql: bool = False) -> list[Statement]:
    """Split SQL text into statements, honouring quotes and comments."""
    statements: list[Statement] = []
    buffer: list[str] = []
    quote: str | None = None       # Active quote char: ' " ` or [
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(text)

    def flush(vertical: bool = False, repeat: int = 1) -> None:
        sql = "".join(buffer).strip()
        buffer.clear()
        if sql:
            statements.append(
                Statement(sql=sql, vertical=vertical, repeat=repeat)
            )

    def at_line_start() -> bool:
        """Whether only whitespace stands between here and the line's start."""
        pending = "".join(buffer)
        return pending[pending.rfind("\n") + 1:].strip() == ""

    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""

        if in_line_comment:
            # Keep the newline so line numbers in server errors still line up.
            if char == "\n":
                in_line_comment = False
                buffer.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if quote is not None:
            buffer.append(char)
            # Backslash escapes apply inside MySQL string literals but not
            # inside backtick-quoted identifiers, and not in T-SQL at all.
            if not tsql and char == "\\" and quote in ("'", '"') and nxt:
                buffer.append(nxt)
                index += 2
                continue
            if char == _closing(quote):
                # A doubled quote is an escaped quote, not the end.
                if nxt == _closing(quote):
                    buffer.append(nxt)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        # --- normal code ---
        if char in _openers(tsql):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "-" and nxt == "-" and _line_comment_starts(text, index, tsql):
            in_line_comment = True
            index += 2
            continue
        if char == "#" and not tsql:
            in_line_comment = True
            index += 1
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        if char == ";":
            flush()
            index += 1
            continue
        if tsql and char in "gG" and at_line_start():
            match = _GO_LINE.match(text, index)
            if match:
                flush(repeat=max(1, int(match.group(1) or 1)))
                index = match.end()
                continue
        if not tsql and char == "\\" and nxt in ("G", "g"):
            flush(vertical=True)
            index += 2
            continue
        buffer.append(char)
        index += 1

    # Trailing text with no terminator still counts as a statement; the console
    # only calls this once the user has committed the input.
    flush()
    return statements


def is_complete(text: str, *, tsql: bool = False) -> bool:
    """Whether the buffered input looks terminated.

    Used by the console to decide between running the input and showing a
    continuation prompt. MySQL ends a statement with ";" or ``\\G``; T-SQL
    accepts either ";" or a bare ``GO`` line, so muscle memory from sqlcmd and
    from every other SQL client both work.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    if tsql:
        last = stripped.rsplit("\n", 1)[-1]
        if _GO_LINE.fullmatch(last) and not _ends_inside_quote(
            stripped, tsql=True
        ):
            return True
    if stripped.endswith(";") or (not tsql and stripped.endswith(("\\G", "\\g"))):
        # Only complete if that terminator is real code, not inside a quote.
        return not _ends_inside_quote(stripped, tsql=tsql)
    return False


def _openers(tsql: bool) -> tuple[str, ...]:
    """The characters that begin a quoted run in this dialect."""
    return ("'", '"', "[") if tsql else ("'", '"', "`")


def _closing(opener: str) -> str:
    """The character that ends the run this one began."""
    return "]" if opener == "[" else opener


def _line_comment_starts(text: str, index: int, tsql: bool) -> bool:
    """Whether the "--" at ``index`` begins a comment.

    MySQL wants whitespace after it, so ``5--1`` is arithmetic. T-SQL has no
    such rule: "--" always comments to the end of the line.
    """
    if tsql:
        return True
    return index + 2 >= len(text) or text[index + 2] in " \t\r\n"


def _ends_inside_quote(text: str, *, tsql: bool = False) -> bool:
    """Whether the text ends with an unterminated quote or block comment."""
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if not tsql and char == "\\" and quote in ("'", '"') and nxt:
                index += 2
                continue
            if char == _closing(quote):
                if nxt == _closing(quote):
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in _openers(tsql):
            quote = char
        elif char == "-" and nxt == "-" and _line_comment_starts(text, index, tsql):
            in_line_comment = True
            index += 2
            continue
        elif char == "#" and not tsql:
            in_line_comment = True
        elif char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        index += 1
    return quote is not None or in_block_comment
