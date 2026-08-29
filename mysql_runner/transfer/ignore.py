"""Deploy-ignore engine: gitignore syntax, used for syncs and comparisons.

Batch uploads and two-sided comparisons are close to useless if they drag
``node_modules/``, ``vendor/`` and ``.git/`` along, so both consult a rule set
built from the same syntax everyone already knows. Rules come from a
``.deployignore`` beside the files, from ``.gitignore`` when there is no
``.deployignore``, and from a built-in default list.

Supported, per gitignore(5): comments, blank lines, negation with ``!``,
directory-only rules with a trailing ``/``, anchoring with a leading or
embedded ``/``, ``*`` and ``?`` (which never cross a ``/``), ``**`` for any
number of directories, and character classes.

One inherited limitation is worth stating outright, because it surprises
people: a negation cannot bring back a file whose *parent directory* is
excluded. ``cache/`` followed by ``!cache/.gitkeep`` keeps nothing, exactly as
in git - exclude ``cache/*`` instead if that is what you meant.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

#: Ignore files looked for in a directory, in order of preference.
IGNORE_FILES = (".deployignore", ".gitignore")

#: Applied unless the user turns the built-ins off - the folders nobody means
#: to deploy.
DEFAULT_PATTERNS = (
    ".git/",
    ".svn/",
    ".hg/",
    "node_modules/",
    "vendor/",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    ".env",
    ".DS_Store",
    "Thumbs.db",
    "*.tmp",
    "*.swp",
    ".idea/",
    ".vscode/",
    "cache/",
    "storage/framework/cache/",
)


@dataclass(frozen=True)
class Rule:
    """One compiled ignore pattern."""

    pattern: str
    regex: re.Pattern
    negated: bool
    dir_only: bool

    def matches(self, relpath: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        return self.regex.match(relpath) is not None


@dataclass
class IgnoreRules:
    """An ordered rule set; later rules win, exactly like gitignore."""

    rules: list[Rule] = field(default_factory=list)
    #: Exact relative paths to let through whatever the rules say. This is
    #: not a pattern and deliberately not one: it records a decision the
    #: user has already been asked about by name ("send .env anyway"), and
    #: a name full of glob characters must not quietly widen it.
    allowed: frozenset = frozenset()

    # ----- construction ---------------------------------------------------
    @classmethod
    def from_lines(cls, lines, *, with_defaults: bool = False) -> "IgnoreRules":
        rules: list[Rule] = []
        if with_defaults:
            rules.extend(_compile_all(DEFAULT_PATTERNS))
        rules.extend(_compile_all(lines))
        return cls(rules)

    @classmethod
    def from_text(cls, text: str, *, with_defaults: bool = False) -> "IgnoreRules":
        return cls.from_lines(text.splitlines(), with_defaults=with_defaults)

    @classmethod
    def defaults(cls) -> "IgnoreRules":
        return cls(_compile_all(DEFAULT_PATTERNS))

    @classmethod
    def empty(cls) -> "IgnoreRules":
        return cls([])

    @classmethod
    def from_local_dir(cls, directory: str, *, with_defaults: bool = True) -> "IgnoreRules":
        """Load the first ignore file found in ``directory``."""
        for name in IGNORE_FILES:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                try:
                    text = open(path, encoding="utf-8", errors="replace").read()
                except OSError:
                    break
                return cls.from_text(text, with_defaults=with_defaults)
        return cls.defaults() if with_defaults else cls.empty()

    def extend(self, other: "IgnoreRules") -> "IgnoreRules":
        """A new rule set with ``other``'s rules applied after this one's."""
        return IgnoreRules(
            list(self.rules) + list(other.rules), self.allowed | other.allowed
        )

    def allowing(self, names) -> "IgnoreRules":
        """A copy that lets these exact entries through.

        The rules exist to stop a bulk push carrying things nobody meant to
        deploy, which is the right default for anything automatic. It is the
        wrong answer to someone who has selected one file and pressed Upload:
        ``.env`` is on the built-in list, so picking it did nothing at all and
        said nothing about why. This is how the caller records "yes, that one,
        I meant it" without switching the rules off for everything else.
        """
        clean = {_normalize(name) for name in names}
        return IgnoreRules(
            list(self.rules), self.allowed | {name for name in clean if name}
        )

    # ----- matching -------------------------------------------------------
    def is_ignored(self, relpath: str, *, is_dir: bool = False) -> bool:
        """Whether a path relative to the sync root is excluded.

        An excluded directory takes everything beneath it, and a negation
        inside it cannot bring those files back - the same reason git will not
        descend into an ignored directory.
        """
        clean = _normalize(relpath)
        if not clean:
            return False
        if clean in self.allowed:
            return False
        parts = clean.split("/")
        for depth in range(1, len(parts) + 1):
            candidate = "/".join(parts[:depth])
            leaf = depth == len(parts)
            if self._decide(candidate, is_dir if leaf else True):
                return True
        return False

    def _decide(self, relpath: str, is_dir: bool) -> bool:
        """Last matching rule wins; True means excluded."""
        verdict = False
        for rule in self.rules:
            if rule.matches(relpath, is_dir):
                verdict = not rule.negated
        return verdict

    def filter_names(self, base_rel: str, names, *, dirs=()) -> list[str]:
        """Keep the entries of one directory that survive the rules."""
        dir_set = set(dirs)
        kept: list[str] = []
        for name in names:
            rel = f"{base_rel}/{name}" if base_rel else name
            if not self.is_ignored(rel, is_dir=name in dir_set):
                kept.append(name)
        return kept

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


# ----- writing ------------------------------------------------------------
#: Header written above the first rule this app adds, so a hand-edited file
#: still says where the machine-written half came from.
_ADDED_HEADER = "# Added from Sitekeeper"


def pattern_for(root: str, path: str, *, is_dir: bool) -> str:
    """The rule that excludes exactly ``path`` and nothing else.

    Anchored with a leading ``/`` so ``/config/db.php`` does not also silence
    a ``db.php`` three folders down, and given a trailing ``/`` for a folder,
    which is what makes the rule take everything below it as well.
    """
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith(".."):
        return ""
    return f"/{rel}/" if is_dir else f"/{rel}"


def ignore_file_path(directory: str) -> str:
    """Where a rule added to ``directory`` goes: its ``.deployignore``.

    Deliberately never ``.gitignore``: this app reads that file when there is
    no ``.deployignore``, but writing to it would put a deploy decision into
    the repository's own ignore list, where it changes what git tracks.
    """
    return os.path.join(directory, IGNORE_FILES[0])


def add_patterns(directory: str, patterns) -> tuple[str, list[str]]:
    """Append rules to ``directory``'s .deployignore, creating it if need be.

    Returns the file written and the patterns that were actually new; one
    already in the file is not added twice, so right-clicking the same folder
    again is harmless rather than a growing pile of duplicates.
    """
    path = ignore_file_path(directory)
    try:
        existing = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        existing = ""
    present = {line.strip() for line in existing.splitlines() if line.strip()}
    fresh = []
    for pattern in patterns:
        pattern = str(pattern).strip()
        if pattern and pattern not in present and pattern not in fresh:
            fresh.append(pattern)
    if not fresh:
        return path, []
    body = existing
    if body and not body.endswith("\n"):
        body += "\n"
    if _ADDED_HEADER not in existing:
        body += f"\n{_ADDED_HEADER}\n" if body else f"{_ADDED_HEADER}\n"
    body += "\n".join(fresh) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    return path, fresh


# ----- compilation --------------------------------------------------------
def _compile_all(lines) -> list[Rule]:
    rules: list[Rule] = []
    for line in lines:
        rule = compile_pattern(line)
        if rule is not None:
            rules.append(rule)
    return rules


def compile_pattern(line: str) -> Rule | None:
    """Compile one gitignore line, or return None for blanks and comments."""
    raw = line
    if not raw.strip():
        return None
    if raw.lstrip().startswith("#"):
        return None
    pattern = _strip_trailing_space(raw)
    if not pattern:
        return None

    negated = False
    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]
    elif pattern.startswith(("\\!", "\\#")):
        pattern = pattern[1:]
    if not pattern:
        return None

    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    if not pattern:
        return None

    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    pattern = pattern.lstrip("/")
    body = _translate(pattern)
    prefix = "" if anchored else r"(?:.*/)?"
    regex = re.compile(f"^{prefix}{body}$")
    return Rule(pattern=raw.strip(), regex=regex, negated=negated, dir_only=dir_only)


def _strip_trailing_space(pattern: str) -> str:
    """Drop unescaped trailing whitespace, keeping an escaped final space."""
    index = len(pattern)
    while index > 0 and pattern[index - 1] in " \t":
        if index >= 2 and pattern[index - 2] == "\\":
            break
        index -= 1
    return pattern[:index]


def _translate(pattern: str) -> str:
    """Turn one gitignore pattern body into a regular expression body."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        piece, index = _translate_token(pattern, index)
        out.append(piece)
    body = "".join(out)
    # A directory rule such as "build" must also swallow its contents.
    return body if body.endswith(".*") else f"{body}(?:/.*)?"


def _translate_token(pattern: str, index: int) -> tuple[str, int]:
    """Translate the glob token at ``index``; return (regex, next index)."""
    char = pattern[index]
    if char == "\\" and index + 1 < len(pattern):
        return re.escape(pattern[index + 1]), index + 2
    if pattern.startswith("**", index):
        after = pattern[index + 2: index + 3]
        if after == "/":
            return r"(?:[^/]+/)*", index + 3
        if not after:
            # A trailing "**" takes everything below, if anything.
            return r".*", index + 2
        # "**" glued to text behaves like a plain "*".
        return r"[^/]*", index + 2
    if char == "*":
        return r"[^/]*", index + 1
    if char == "?":
        return r"[^/]", index + 1
    if char == "[":
        closing = pattern.find("]", index + 1)
        if closing == -1:
            return re.escape(char), index + 1
        body = pattern[index + 1: closing]
        if body.startswith("!"):
            body = "^" + body[1:]
        return f"[{body}]", closing + 1
    if char == "/":
        return "/", index + 1
    return re.escape(char), index + 1


def _normalize(relpath: str) -> str:
    return "/".join(
        part for part in relpath.replace("\\", "/").split("/") if part not in ("", ".")
    )
