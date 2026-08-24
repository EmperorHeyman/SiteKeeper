"""Connection strings, and import/export of WinSCP sessions.

Three formats, all of them round-trippable:

* a URL - ``sftp://user:secret@host:2202/var/www`` - which is what people paste
  to each other,
* a plain list of those URLs, one per line, and
* ``WinSCP.ini``, so an existing WinSCP install can be adopted wholesale.

WinSCP stores session passwords with a reversible obfuscation, not encryption -
it is a scramble keyed by the user and host, published in WinSCP's own source.
It is implemented here so an import brings the passwords along; the values land
in this app's encrypted vault, which is a step up from where they came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote as url_quote, unquote, urlsplit

from mysql_runner.storage.models import ConnectionKind, Environment, ServerProfile

#: URL schemes understood on the way in.
SCHEMES = {
    "sftp": ConnectionKind.SFTP,
    "ssh": ConnectionKind.SFTP,
    "scp": ConnectionKind.SFTP,
    "ftp": ConnectionKind.FTP,
    "ftps": ConnectionKind.FTPS,
    "ftpes": ConnectionKind.FTPS,
    "mysql": ConnectionKind.MYSQL,
    "http": ConnectionKind.PHPMYADMIN,
    "https": ConnectionKind.PHPMYADMIN,
}

#: The scheme written back out for each kind.
_KIND_SCHEME = {
    ConnectionKind.SFTP: "sftp",
    ConnectionKind.FTP: "ftp",
    ConnectionKind.FTPS: "ftps",
    ConnectionKind.MYSQL: "mysql",
}


class ConnStrError(Exception):
    """Raised when a connection string or session file cannot be read."""


# ----- URLs ---------------------------------------------------------------
def parse_url(text: str, *, label: str = "") -> ServerProfile:
    """Turn one connection string into a profile."""
    raw = text.strip()
    if not raw:
        raise ConnStrError("Empty connection string.")
    if "://" not in raw:
        raw = f"sftp://{raw}"  # Bare host:port/path is assumed to be SFTP.
    parts = urlsplit(raw)
    kind = SCHEMES.get(parts.scheme.lower())
    if kind is None:
        raise ConnStrError(f"Unsupported scheme {parts.scheme!r}.")
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if kind == ConnectionKind.PHPMYADMIN:
        return ServerProfile(
            label=label or parts.netloc,
            kind=kind,
            url=raw,
            username=username,
            password=password,
        )
    host = parts.hostname or ""
    if not host:
        raise ConnStrError("The connection string has no host.")
    path = unquote(parts.path or "")
    profile = ServerProfile(
        label=label or host,
        kind=kind,
        host=host,
        port=parts.port or 0,
        username=username,
        password=password,
    )
    if kind == ConnectionKind.MYSQL:
        profile.database = path.lstrip("/")
    elif path not in ("", "/"):
        profile.remote_dir = path.rstrip("/")
    return profile


def to_url(profile: ServerProfile, *, include_password: bool = False) -> str:
    """Render a profile as a connection string."""
    if profile.kind == ConnectionKind.PHPMYADMIN:
        return profile.url
    scheme = _KIND_SCHEME.get(profile.kind, "sftp")
    credentials = ""
    if profile.username:
        credentials = url_quote(profile.username, safe="")
        if include_password and profile.password:
            credentials += ":" + url_quote(profile.password, safe="")
        credentials += "@"
    port = f":{profile.port}" if profile.port else ""
    if profile.kind == ConnectionKind.MYSQL:
        tail = f"/{profile.database}" if profile.database else ""
    else:
        tail = profile.remote_dir if profile.remote_dir.startswith("/") else ""
    return f"{scheme}://{credentials}{profile.host}{port}{tail}"


def parse_url_list(text: str) -> tuple[list[ServerProfile], list[str]]:
    """Parse a file of connection strings. Returns (profiles, problems).

    A line may be prefixed with a label: ``prod = sftp://…``.
    """
    profiles: list[ServerProfile] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        label = ""
        body = stripped
        if "=" in stripped and "://" in stripped.split("=", 1)[1]:
            label, body = (part.strip() for part in stripped.split("=", 1))
        try:
            profiles.append(parse_url(body, label=label))
        except ConnStrError as exc:
            problems.append(f"line {number}: {exc}")
    return profiles, problems


def to_url_list(profiles: list[ServerProfile], *, include_passwords: bool = False) -> str:
    """Render profiles as a labelled connection-string list."""
    lines = ["# Sitekeeper connections"]
    for profile in profiles:
        url = to_url(profile, include_password=include_passwords)
        if not url:
            continue
        lines.append(f"{profile.label} = {url}")
    return "\n".join(lines) + "\n"


# ----- WinSCP password obfuscation ---------------------------------------
_PW_MAGIC = 0xA3
_PW_FLAG = 0xFF
_HEX = "0123456789ABCDEF"


def _decode_byte(text: str, index: int) -> tuple[int, int]:
    """Decode one obfuscated byte at ``index``. Returns (value, next index)."""
    if index + 1 >= len(text):
        raise ConnStrError("Truncated WinSCP password.")
    high = _HEX.find(text[index].upper())
    low = _HEX.find(text[index + 1].upper())
    if high < 0 or low < 0:
        raise ConnStrError("A WinSCP password contains non-hex characters.")
    value = ~(((high << 4) + low) ^ _PW_MAGIC) & 0xFF
    return value, index + 2


def _encode_byte(value: int) -> str:
    scrambled = (~value & 0xFF) ^ _PW_MAGIC
    return _HEX[(scrambled >> 4) & 0x0F] + _HEX[scrambled & 0x0F]


def decode_winscp_password(stored: str, username: str, host: str) -> str:
    """Recover a session password from WinSCP's stored form.

    Returns "" when the value was saved for a different user or host, which is
    what WinSCP itself does rather than handing back nonsense.
    """
    text = (stored or "").strip()
    if not text:
        return ""
    try:
        flag, index = _decode_byte(text, 0)
        if flag == _PW_FLAG:
            _dummy, index = _decode_byte(text, index)
            length, index = _decode_byte(text, index)
        else:
            length = flag
        padding, index = _decode_byte(text, index)
        index += padding * 2
        out: list[str] = []
        for _ in range(length):
            value, index = _decode_byte(text, index)
            out.append(chr(value))
    except ConnStrError:
        return ""
    result = "".join(out)
    if flag == _PW_FLAG:
        key = f"{username}{host}"
        if not result.startswith(key):
            return ""  # Saved under a different account; the password is not ours.
        result = result[len(key):]
    return result


def encode_winscp_password(password: str, username: str, host: str) -> str:
    """Store a password the way WinSCP does, so an export can be re-imported."""
    if not password:
        return ""
    payload = f"{username}{host}{password}"
    out = [
        _encode_byte(_PW_FLAG),
        _encode_byte(0x00),          # WinSCP writes a random filler byte here.
        _encode_byte(len(payload) & 0xFF),
        _encode_byte(0x00),          # No leading padding.
    ]
    out.extend(_encode_byte(ord(char) & 0xFF) for char in payload)
    return "".join(out)


# ----- WinSCP.ini --------------------------------------------------------
#: WinSCP's FSProtocol values, as written in its ini file.
_FS_SCP = 0
_FS_SFTP = 1
_FS_SFTP_ONLY = 2
_FS_FTP = 5
_FS_WEBDAV = 6
_FS_S3 = 7

_PROTOCOL_NAMES = {_FS_WEBDAV: "WebDAV", _FS_S3: "S3"}


@dataclass
class WinScpImport:
    """The outcome of reading a WinSCP.ini."""

    profiles: list[ServerProfile]
    skipped: list[str]

    def summary(self) -> str:
        text = f"{len(self.profiles)} session(s) read"
        if self.skipped:
            text += f", {len(self.skipped)} skipped"
        return text


def parse_winscp_ini(text: str) -> WinScpImport:
    """Read every usable session out of a WinSCP.ini."""
    profiles: list[ServerProfile] = []
    skipped: list[str] = []
    for name, values in _ini_sections(text):
        if not name.lower().startswith("sessions\\"):
            continue
        session = unquote(name.split("\\", 1)[1])
        if not session or session.lower() == "default settings":
            continue
        profile = _profile_from_session(session, values, skipped)
        if profile is not None:
            profiles.append(profile)
    return WinScpImport(profiles=profiles, skipped=skipped)


def _profile_from_session(
    session: str, values: dict[str, str], skipped: list[str]
) -> ServerProfile | None:
    host = values.get("hostname", "").strip()
    if not host:
        skipped.append(f"{session}: no host name")
        return None
    protocol = _as_int(values.get("fsprotocol"), _FS_SFTP_ONLY)
    if protocol in _PROTOCOL_NAMES:
        skipped.append(f"{session}: {_PROTOCOL_NAMES[protocol]} is not supported")
        return None
    if protocol == _FS_FTP:
        kind = ConnectionKind.FTPS if _as_int(values.get("ftps"), 0) else ConnectionKind.FTP
    elif protocol in (_FS_SCP, _FS_SFTP, _FS_SFTP_ONLY):
        kind = ConnectionKind.SFTP
    else:
        skipped.append(f"{session}: unknown protocol {protocol}")
        return None

    username = values.get("username", "")
    password = decode_winscp_password(values.get("password", ""), username, host)
    remote_dir = _clean_dir(values.get("remotedirectory", ""))
    local_dir = _clean_dir(values.get("localdirectory", ""), windows=True)
    # WinSCP nests sessions with "/" in the name; that maps to our group.
    group = session.rsplit("/", 1)[0] if "/" in session else ""
    return ServerProfile(
        label=session.replace("/", " / "),
        kind=kind,
        host=host,
        port=_as_int(values.get("portnumber"), 0),
        username=username,
        password=password,
        remote_dir=remote_dir,
        local_dir=local_dir,
        private_key_path=_clean_dir(values.get("publickeyfile", ""), windows=True),
        passive=bool(_as_int(values.get("ftppasvmode"), 1)),
        group=group,
        environment=_guess_environment(session),
    )


def to_winscp_ini(profiles: list[ServerProfile], *, include_passwords: bool = True) -> str:
    """Render profiles as a WinSCP.ini that WinSCP can import."""
    lines = ["[Configuration\\Interface]", "; Written by Sitekeeper", ""]
    for profile in profiles:
        if not profile.kind.is_transfer:
            continue
        lines.extend(_session_lines(profile, include_passwords))
    return "\n".join(lines)


def _session_lines(profile: ServerProfile, include_passwords: bool) -> list[str]:
    """The ini block for one session."""
    name = url_quote(profile.label.replace(" / ", "/"), safe="/")
    lines = [
        f"[Sessions\\{name}]",
        f"HostName={profile.host}",
        f"FSProtocol={_fs_protocol(profile.kind)}",
    ]
    optional = (
        ("PortNumber", str(profile.port) if profile.port else ""),
        ("UserName", profile.username),
        ("Ftps", "2" if profile.kind == ConnectionKind.FTPS else ""),
        ("RemoteDirectory", profile.remote_dir),
        ("LocalDirectory", profile.local_dir),
        ("PublicKeyFile", profile.private_key_path),
    )
    lines.extend(f"{key}={value}" for key, value in optional if value)
    if include_passwords and profile.password:
        stored = encode_winscp_password(profile.password, profile.username, profile.host)
        lines.append(f"Password={stored}")
    lines.append("")
    return lines


def _fs_protocol(kind: ConnectionKind) -> int:
    if kind in (ConnectionKind.FTP, ConnectionKind.FTPS):
        return _FS_FTP
    return _FS_SFTP_ONLY


def _ini_sections(text: str):
    """Yield (section name, {lowercased key: value}) pairs from an ini file."""
    section = ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if section:
                yield section, values
            section = stripped[1:-1]
            values = {}
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip().lower()] = value.strip()
    if section:
        yield section, values


def _clean_dir(value: str, *, windows: bool = False) -> str:
    text = unquote(value.strip())
    if not text:
        return ""
    if windows:
        return text
    return "/" if text == "/" else "/" + text.strip("/")


def _guess_environment(name: str) -> Environment:
    """Tint sessions whose name says what they are - a free production guard."""
    lowered = name.lower()
    if re.search(r"\b(prod|production|live|www)\b", lowered):
        return Environment.PROD
    if re.search(r"\b(stag|staging|preprod|uat)\b", lowered):
        return Environment.STAGING
    if re.search(r"\b(dev|devel|local|test|sandbox)\b", lowered):
        return Environment.DEV
    return Environment.NONE


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ----- WinSCP on this machine -------------------------------------------
#: Where an installed WinSCP keeps its sessions when it is not using an ini.
_REGISTRY_KEY = r"Software\Martin Prikryl\WinSCP 2\Sessions"

#: Ini locations WinSCP uses, in the order it looks for them.
_INI_CANDIDATES = (
    ("APPDATA", "WinSCP.ini"),
    ("LOCALAPPDATA", "WinSCP.ini"),
    ("ProgramFiles(x86)", r"WinSCP\WinSCP.ini"),
    ("ProgramFiles", r"WinSCP\WinSCP.ini"),
    ("USERPROFILE", r"Documents\WinSCP.ini"),
)


def find_winscp_ini() -> str:
    """The first WinSCP.ini that exists on this machine, or ""."""
    import os

    for variable, tail in _INI_CANDIDATES:
        base = os.environ.get(variable, "")
        if not base:
            continue
        candidate = os.path.join(base, tail)
        if os.path.isfile(candidate):
            return candidate
    return ""


def read_winscp_registry() -> WinScpImport:
    """Read WinSCP's sessions out of the Windows registry.

    An installed WinSCP stores sessions under HKCU by default and only uses an
    ini file when told to, so looking for the ini alone finds nothing for most
    people. The values are the same names as in the ini, so both go through the
    same builder.
    """
    profiles: list[ServerProfile] = []
    skipped: list[str] = []
    try:
        import winreg
    except ImportError:  # not Windows
        return WinScpImport(profiles=profiles, skipped=skipped)
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY)
    except OSError:
        return WinScpImport(profiles=profiles, skipped=skipped)
    with root:
        for index in range(_subkey_count(winreg, root)):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            session = unquote(name)
            if session.lower() == "default settings":
                continue
            values = _registry_values(winreg, root, name)
            if values is None:
                skipped.append(f"{session}: could not be read")
                continue
            profile = _profile_from_session(session, values, skipped)
            if profile is not None:
                profiles.append(profile)
    return WinScpImport(profiles=profiles, skipped=skipped)


def _subkey_count(winreg, key) -> int:
    try:
        return winreg.QueryInfoKey(key)[0]
    except OSError:
        return 0


def _registry_values(winreg, root, name: str) -> dict[str, str] | None:
    """Every value of one session key, keyed the way the ini parser expects."""
    try:
        handle = winreg.OpenKey(root, name)
    except OSError:
        return None
    values: dict[str, str] = {}
    with handle:
        count = 0
        try:
            count = winreg.QueryInfoKey(handle)[1]
        except OSError:
            return values
        for index in range(count):
            try:
                key, value, _kind = winreg.EnumValue(handle, index)
            except OSError:
                break
            values[key.strip().lower()] = str(value)
    return values


def discover_winscp() -> tuple[str, WinScpImport]:
    """Find WinSCP's sessions on this machine.

    Returns (where they came from, what was found). The registry is tried first
    because that is where an installed WinSCP puts them.
    """
    from_registry = read_winscp_registry()
    if from_registry.profiles:
        return "the Windows registry", from_registry
    ini = find_winscp_ini()
    if ini:
        try:
            return ini, parse_winscp_ini(open(ini, encoding="utf-8", errors="replace").read())
        except OSError as exc:
            return ini, WinScpImport(profiles=[], skipped=[str(exc)])
    return "", WinScpImport(profiles=[], skipped=from_registry.skipped)


# ----- dispatch ----------------------------------------------------------
def load_any(path: str) -> WinScpImport:
    """Import connections from a WinSCP.ini or a list of connection strings."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        raise ConnStrError(str(exc)) from exc
    if "[Sessions\\" in text or "[Configuration\\" in text:
        return parse_winscp_ini(text)
    profiles, problems = parse_url_list(text)
    if not profiles and problems:
        raise ConnStrError(problems[0])
    return WinScpImport(profiles=profiles, skipped=problems)
