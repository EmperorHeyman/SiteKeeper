"""POSIX permission arithmetic and the presets behind the chmod dialog.

Nothing here talks to a server; it is the octal/symbolic conversion and the
common presets, kept separate so the dialog is a thin shell over tested code.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The three permission groups, in the order they appear in a mode.
WHO = ("owner", "group", "other")
#: The three bits within a group.
WHAT = ("read", "write", "execute")

_SHIFT = {"owner": 6, "group": 3, "other": 0}
_BIT = {"read": 0b100, "write": 0b010, "execute": 0b001}

#: Special bits above the nine ordinary ones.
SETUID = 0o4000
SETGID = 0o2000
STICKY = 0o1000


@dataclass(frozen=True)
class Preset:
    """One entry in the quick-permissions menu."""

    label: str
    mode: int
    note: str
    #: Applied to directories only, files only, or both.
    scope: str = "all"
    #: Shown with a warning colour in the UI.
    risky: bool = False


#: What people actually set, in the order a web deploy needs them.
PRESETS = (
    Preset("Web folders (755)", 0o755, "Owner writes; everyone may enter and read", "dirs"),
    Preset("Web files (644)", 0o644, "Owner writes; everyone may read", "files"),
    Preset("Shared group (775)", 0o775, "Owner and group write", "dirs"),
    Preset("Shared files (664)", 0o664, "Owner and group write", "files"),
    Preset("Private folder (700)", 0o700, "Only the owner, and nobody else"),
    Preset("Private file (600)", 0o600, "Only the owner, and nobody else"),
    Preset("SSH key (400)", 0o400, "Read-only for the owner - required by ssh", "files"),
    Preset("Executable (755)", 0o755, "A script or binary anyone may run", "files"),
    Preset("World-writable (777)", 0o777, "Anyone may change anything - avoid", "all", True),
)


def to_octal(mode: int, *, digits: int = 3) -> str:
    """"755", or "4755" when a special bit is set."""
    value = mode & 0o7777
    if value > 0o777 or digits == 4:
        return format(value, "04o")
    return format(value, "03o")


def parse_octal(text: str) -> int:
    """Parse "755", "0755" or "4755" into a mode. Raises ValueError otherwise."""
    cleaned = text.strip().lstrip("0o").strip() or "0"
    if not cleaned.isdigit() or any(char not in "01234567" for char in cleaned):
        raise ValueError(f"{text!r} is not an octal permission value.")
    if len(cleaned) > 4:
        raise ValueError("A permission value has at most four digits.")
    return int(cleaned, 8) & 0o7777


def format_symbolic(mode: int, *, is_dir: bool = False) -> str:
    """"drwxr-xr-x" - the ls(1) form."""
    out = ["d" if is_dir else "-"]
    for who in WHO:
        shift = _SHIFT[who]
        group = (mode >> shift) & 0b111
        out.append("r" if group & _BIT["read"] else "-")
        out.append("w" if group & _BIT["write"] else "-")
        execute = bool(group & _BIT["execute"])
        special = _special_char(mode, who, execute)
        out.append(special if special else ("x" if execute else "-"))
    return "".join(out)


def _special_char(mode: int, who: str, execute: bool) -> str:
    if who == "owner" and mode & SETUID:
        return "s" if execute else "S"
    if who == "group" and mode & SETGID:
        return "s" if execute else "S"
    if who == "other" and mode & STICKY:
        return "t" if execute else "T"
    return ""


def parse_symbolic(text: str) -> int:
    """Parse "rwxr-xr-x" (with or without a leading type character)."""
    body = text.strip()
    if len(body) == 10:
        body = body[1:]
    if len(body) != 9:
        raise ValueError("A symbolic permission string has nine characters.")
    mode = 0
    for index, char in enumerate(body):
        who = WHO[index // 3]
        what = WHAT[index % 3]
        if char == "-":
            continue
        expected = {"read": "r", "write": "w", "execute": "x"}[what]
        if char == expected:
            mode |= _BIT[what] << _SHIFT[who]
            continue
        if what == "execute" and char in "sStT":
            if char in "sS":
                mode |= SETUID if who == "owner" else SETGID
            else:
                mode |= STICKY
            if char in "st":
                mode |= _BIT["execute"] << _SHIFT[who]
            continue
        raise ValueError(f"{char!r} is not valid at position {index + 1}.")
    return mode


def has_bit(mode: int, who: str, what: str) -> bool:
    return bool((mode >> _SHIFT[who]) & _BIT[what])


def with_bit(mode: int, who: str, what: str, enabled: bool) -> int:
    """Return ``mode`` with one checkbox flipped."""
    bit = _BIT[what] << _SHIFT[who]
    return (mode | bit) if enabled else (mode & ~bit)


def grid(mode: int) -> dict[str, dict[str, bool]]:
    """The nine checkboxes, ready for a dialog to render."""
    return {who: {what: has_bit(mode, who, what) for what in WHAT} for who in WHO}


def describe(mode: int, *, is_dir: bool = False) -> str:
    """"755 (rwxr-xr-x)" - what goes in a status line."""
    return f"{to_octal(mode)} ({format_symbolic(mode, is_dir=is_dir)[1:]})"


def is_risky(mode: int) -> bool:
    """Whether a mode deserves a warning before it is applied."""
    world_writable = has_bit(mode, "other", "write")
    return world_writable or bool(mode & (SETUID | SETGID))


def suggest(*, is_dir: bool) -> int:
    """The sane default for a new folder or file on a web host."""
    return 0o755 if is_dir else 0o644
