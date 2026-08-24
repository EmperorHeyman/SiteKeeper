"""Render query results the way the mysql command-line client does.

The console tab is meant to feel like a real mysql shell, so results are drawn
as ASCII tables (or the vertical \\G layout) rather than as a widget grid.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

NULL = "NULL"
#: Types mysql right-aligns in its own output.
_NUMERIC = (int, float, Decimal)


def render_value(value: object) -> str:
    """Format one cell the way the mysql client prints it."""
    if value is None:
        return NULL
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            # Binary column: show hex like mysql --binary-as-hex.
            return "0x" + value.hex().upper()
    if isinstance(value, (datetime, date)):
        return str(value)
    if isinstance(value, timedelta):
        # MySQL TIME values arrive as timedelta; show them as HH:MM:SS.
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return str(value)


def _is_numeric_column(rows: list[tuple], index: int) -> bool:
    """Whether every non-NULL value in a column is a number."""
    seen = False
    for row in rows:
        value = row[index] if index < len(row) else None
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, _NUMERIC):
            return False
        seen = True
    return seen


def _flatten(text: str) -> str:
    """Collapse newlines: a wrapped value would break the table frame."""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def format_table(columns: list[str], rows: list[tuple]) -> str:
    """Render a result set as a bordered ASCII table."""
    if not columns:
        return ""
    # Flatten before measuring, so the widths match what actually gets drawn.
    cells = [[_flatten(render_value(row[i] if i < len(row) else None))
              for i in range(len(columns))] for row in rows]
    headers = [_flatten(name) for name in columns]
    widths = [
        max([len(headers[index])] + [len(row[index]) for row in cells])
        for index in range(len(columns))
    ]
    numeric = [_is_numeric_column(rows, i) for i in range(len(columns))]

    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [rule, _format_row(headers, widths, [False] * len(columns)), rule]
    lines.extend(_format_row(row, widths, numeric) for row in cells)
    lines.append(rule)
    return "\n".join(lines)


def _format_row(values: list[str], widths: list[int], numeric: list[bool]) -> str:
    parts = [
        value.rjust(widths[index]) if numeric[index] else value.ljust(widths[index])
        for index, value in enumerate(values)
    ]
    return "| " + " | ".join(parts) + " |"


def format_vertical(columns: list[str], rows: list[tuple]) -> str:
    """Render a result set in the \\G one-field-per-line layout."""
    if not columns:
        return ""
    label_width = max(len(name) for name in columns)
    blocks = []
    for number, row in enumerate(rows, start=1):
        header = f"{'*' * 27} {number}. row {'*' * 27}"
        body = [
            f"{columns[i].rjust(label_width)}: "
            f"{render_value(row[i] if i < len(row) else None)}"
            for i in range(len(columns))
        ]
        blocks.append("\n".join([header] + body))
    return "\n".join(blocks)


def format_summary(rowcount: int, duration_ms: float, is_result_set: bool) -> str:
    """The trailing "N rows in set (0.00 sec)" line."""
    seconds = duration_ms / 1000.0
    noun = "row" if rowcount == 1 else "rows"
    if is_result_set:
        if rowcount == 0:
            return f"Empty set ({seconds:.3f} sec)"
        return f"{rowcount} {noun} in set ({seconds:.3f} sec)"
    affected = max(rowcount, 0)
    noun = "row" if affected == 1 else "rows"
    return f"Query OK, {affected} {noun} affected ({seconds:.3f} sec)"
