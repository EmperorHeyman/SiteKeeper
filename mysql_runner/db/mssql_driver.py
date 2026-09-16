"""Microsoft SQL Server driver access, with no GUI toolkit attached.

The counterpart of driver.py, for the connections SSMS opens. Everything here
is import-light on purpose: the sidecar and the MCP server both load this
module on machines that may have no ODBC driver installed at all, and neither
may fail to start over it.

Why pyodbc rather than a pure-Python driver: whoever asked for this already
has SSMS, and SSMS is installed alongside *Microsoft ODBC Driver for SQL
Server*. So the thing we need to talk to the server is, in practice, already
on the machine and already configured for it - including the Windows
authentication that most SQL Server logins actually use, which a pure-Python
driver cannot do without a Kerberos setup nobody wants to perform.
"""

from __future__ import annotations

import re

#: Wall-clock limit for establishing a connection. Same as MySQL's, so a
#: server that is simply not there fails in the same amount of time.
CONNECT_TIMEOUT = 10

#: Hard cap on rows pulled into memory for one statement.
MAX_ROWS = 5000

#: ODBC drivers we know how to drive, best first. 18 changed the default to
#: Encrypt=yes, which is why the profile carries an encryption choice at all.
_PREFERRED_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

#: The driver's own prefixes: "[Microsoft][ODBC Driver 18 for SQL Server]".
#: Three of them in front of every message, saying nothing about what went
#: wrong, in a console where the useful half then wraps off the line. Anchored
#: to the start of each part, because a message can legitimately contain
#: brackets of its own - "Invalid object name '[dbo].[Orders]'".
_DRIVER_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")
#: The trailing " (208) (SQLExecDirectW)" ODBC adds to a server message.
_ODBC_SUFFIX = re.compile(r"\s*\((?:\d+)\)(?:\s*\(SQL[A-Za-z]+\))?\s*$")


class MSSQLUnavailable(RuntimeError):
    """Raised when SQL Server cannot be reached from this build."""


def import_driver():
    """Import pyodbc lazily so builds without it still start."""
    try:
        import pyodbc  # noqa: PLC0415 - deliberately deferred
    except ImportError as exc:  # pragma: no cover - depends on the build
        raise MSSQLUnavailable(
            "The SQL Server driver (pyodbc) is not available in this build, "
            "so SQL Server console tabs cannot connect."
        ) from exc
    return pyodbc


def driver_available() -> bool:
    """Whether native SQL Server connections are possible in this build."""
    try:
        import_driver()
    except MSSQLUnavailable:
        return False
    return True


def installed_drivers() -> list[str]:
    """The ODBC drivers on this machine that can talk to SQL Server."""
    try:
        pyodbc = import_driver()
    except MSSQLUnavailable:
        return []
    try:
        return [
            name for name in pyodbc.drivers() if "SQL Server" in name
        ]
    except Exception:  # pragma: no cover - a broken ODBC registry
        return []


def default_driver() -> str:
    """The best installed ODBC driver, or "" when there is none."""
    installed = installed_drivers()
    for candidate in _PREFERRED_DRIVERS:
        if candidate in installed:
            return candidate
    return installed[0] if installed else ""


def missing_driver_message() -> str:
    """Why a connection cannot be made, and what to do about it.

    Two different absences with the same symptom, so they are told apart
    here rather than leaving the reader to guess which one they have.
    """
    if not driver_available():
        return (
            "This build has no SQL Server driver (pyodbc), so SQL Server "
            "console tabs cannot connect. Install pyodbc and restart."
        )
    if not installed_drivers():
        return (
            "No Microsoft ODBC driver for SQL Server is installed on this "
            "machine. Install 'ODBC Driver 18 for SQL Server' (it ships with "
            "SSMS and is a free download from Microsoft) and restart."
        )
    return ""


def server_address(host: str, port: int, instance: str = "") -> str:
    """The SERVER= value: host, optionally \\instance, optionally ,port."""
    address = host
    if instance:
        address += "\\" + instance
    if port:
        address += f",{port}"
    return address


def connection_string(
    host: str,
    port: int,
    username: str,
    password: str,
    database: str = "",
    *,
    instance: str = "",
    windows_auth: bool = False,
    encrypt: bool = True,
    trust_certificate: bool = True,
    odbc_driver: str = "",
    application: str = "Sitekeeper",
) -> str:
    """Build an ODBC connection string for these settings.

    ``trust_certificate`` defaults to on because the servers this is aimed at
    - an in-house SQL Server on a LAN, the one SSMS is pointed at all day -
    almost always present a self-signed certificate, and driver 18 refuses
    those outright. Turning it off in the profile is what someone with a real
    certificate does; leaving it on is what everyone else needs to connect at
    all.
    """
    driver = odbc_driver or default_driver()
    if not driver:
        raise MSSQLUnavailable(missing_driver_message() or "No ODBC driver.")
    parts = [
        # The driver name is always braced, the way every Microsoft example
        # writes it; a name with a space in it is not reliably parsed bare.
        "DRIVER={" + driver.replace("}", "}}") + "}",
        f"SERVER={_value(server_address(host, port, instance))}",
    ]
    if database:
        parts.append(f"DATABASE={_value(database)}")
    if windows_auth:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={_value(username)}")
        parts.append(f"PWD={_value(password)}")
    parts.append("Encrypt=" + ("yes" if encrypt else "no"))
    if trust_certificate:
        parts.append("TrustServerCertificate=yes")
    parts.append(f"Connection Timeout={CONNECT_TIMEOUT}")
    parts.append(f"APP={_value(application)}")
    return ";".join(parts)


def _value(text: str) -> str:
    """Quote a connection-string value ODBC's way when it needs it.

    Passwords contain semicolons often enough that not doing this produces a
    login failure nobody can explain: the half after the ";" is read as
    another keyword and quietly dropped.
    """
    if text == "" or any(char in text for char in ";{}=") or text != text.strip():
        return "{" + text.replace("}", "}}") + "}"
    return text


def describe_error(exc: Exception) -> str:
    """Turn a pyodbc exception into something worth reading.

    pyodbc hands back ``('42S02', "[42S02] [Microsoft][ODBC Driver 18 for SQL
    Server][SQL Server]Invalid object name 'foo'. (208) (SQLExecDirectW)")``.
    The sentence in the middle is the whole message; the rest is plumbing.

    A failed login arrives as two of those joined with "; ", each wearing the
    same four brackets, so they are cleaned one at a time and anything that
    turns out to be a repeat is dropped.
    """
    args = getattr(exc, "args", ())
    state = str(args[0]).strip() if args else ""
    raw = str(args[1]) if len(args) >= 2 else str(exc).strip()
    parts: list[str] = []
    for chunk in raw.split("; "):
        cleaned = _ODBC_SUFFIX.sub("", _DRIVER_PREFIX.sub("", chunk).strip())
        cleaned = cleaned.strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    body = " ".join(parts)
    if not body:
        body = exc.__class__.__name__
    if state and state != body:
        return f"ERROR {state}: {body}"
    return body


def database_from_use(sql: str) -> str | None:
    """Return the database named by a USE statement, or None.

    T-SQL's USE is MySQL's, brackets aside: ``USE [Northwind]``.
    """
    head = sql.lstrip().split(None, 1)
    if len(head) == 2 and head[0].lower() == "use":
        return head[1].strip().strip(";").strip().strip("[]`\"'").strip()
    return None
