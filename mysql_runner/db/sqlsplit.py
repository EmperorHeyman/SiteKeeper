"""Split a block of SQL into individual statements.

The console feeds whatever the user typed straight to the server, so it has to
find statement boundaries itself. A naive split on ";" breaks as soon as a
semicolon appears inside a string literal, a quoted identifier, or a comment,
so this walks the text one character at a time and only treats ";" as a
terminator while in normal code.

A trailing "\\G" (the mysql client's vertical-output suffix) is recognised and
reported separately rather than being sent to the server.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Statement:
    """One statement ready to execute."""

    sql: str
    #: True when the user ended it with \\G instead of ";".
    vertical: bool = False


def split_statements(text: str) -> list[Statement]:
    """Split SQL text into statements, honouring quotes and comments."""
    statements: list[Statement] = []
    buffer: list[str] = []
    quote: str | None = None       # Active quote char: ' " or `
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(text)

    def flush(vertical: bool = False) -> None:
        sql = "".join(buffer).strip()
        buffer.clear()
        if sql:
            statements.append(Statement(sql=sql, vertical=vertical))

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
            # inside backtick-quoted identifiers.
            if char == "\\" and quote in ("'", '"') and nxt:
                buffer.append(nxt)
                index += 2
                continue
            if char == quote:
                # A doubled quote is an escaped quote, not the end.
                if nxt == quote:
                    buffer.append(nxt)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        # --- normal code ---
        if char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "-" and nxt == "-" and (index + 2 >= length or text[index + 2] in " \t\r\n"):
            in_line_comment = True
            index += 2
            continue
        if char == "#":
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
        if char == "\\" and nxt in ("G", "g"):
            flush(vertical=True)
            index += 2
            continue
        buffer.append(char)
        index += 1

    # Trailing text with no terminator still counts as a statement; the console
    # only calls this once the user has committed the input.
    flush()
    return statements


def is_complete(text: str) -> bool:
    """Whether the buffered input looks terminated (";" or \\G at the end).

    Used by the console to decide between running the input and showing a
    continuation prompt.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped.endswith((";",)) or stripped.endswith(("\\G", "\\g")):
        # Only complete if that terminator is real code, not inside a quote.
        return not _ends_inside_quote(stripped)
    return False


def _ends_inside_quote(text: str) -> bool:
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
            if char == "\\" and quote in ("'", '"') and nxt:
                index += 2
                continue
            if char == quote:
                if nxt == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "-" and nxt == "-" and (index + 2 >= length or text[index + 2] in " \t\r\n"):
            in_line_comment = True
            index += 2
            continue
        elif char == "#":
            in_line_comment = True
        elif char == "/" and nxt == "*":
            in_block_comment = True
            index += 2
            continue
        index += 1
    return quote is not None or in_block_comment
