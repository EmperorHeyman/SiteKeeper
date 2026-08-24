"""MySQL driver access, with no GUI toolkit attached.

Both front ends need these: the Qt worker in mysql_client.py and the FastAPI
service in backend/app/services. They live here rather than next to the Qt
worker so that importing them does not drag PyQt6 into the sidecar, which is
frozen without any GUI toolkit at all.
"""

from __future__ import annotations

#: Wall-clock limit for establishing a connection.
CONNECT_TIMEOUT = 10
#: Hard cap on rows pulled into memory for one statement. A stray
#: "SELECT * FROM big_table" should not take the app down with it.
MAX_ROWS = 5000


class MySQLUnavailable(RuntimeError):
    """Raised when the MySQL driver is not installed in this build."""


def import_driver():
    """Import PyMySQL lazily so builds without it still start."""
    try:
        import pymysql  # noqa: PLC0415 - deliberately deferred
    except ImportError as exc:  # pragma: no cover - depends on the build
        raise MySQLUnavailable(
            "The MySQL driver (PyMySQL) is not available in this build, so "
            "native SQL console tabs cannot connect."
        ) from exc
    return pymysql


def driver_available() -> bool:
    """Whether native MySQL connections are possible in this build."""
    try:
        import_driver()
    except MySQLUnavailable:
        return False
    return True


def connect_kwargs(
    host: str, port: int, username: str, password: str, database: str = ""
) -> dict:
    """Connection arguments shared by both front ends."""
    kwargs = {
        "host": host,
        "port": port,
        "user": username,
        "password": password,
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": CONNECT_TIMEOUT,
    }
    if database:
        kwargs["database"] = database
    return kwargs


def describe_error(exc: Exception) -> str:
    """Turn a driver exception into a mysql-client-style message."""
    args = getattr(exc, "args", ())
    if len(args) >= 2:
        return f"ERROR {args[0]}: {args[1]}"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def database_from_use(sql: str) -> str | None:
    """Return the schema named by a USE statement, or None."""
    head = sql.lstrip().split(None, 1)
    if len(head) == 2 and head[0].lower() == "use":
        return head[1].strip().strip("`;").strip()
    return None
