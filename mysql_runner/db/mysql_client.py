"""Native MySQL connection worker.

The console tab talks to MySQL directly (port 3306) instead of driving
phpMyAdmin, so every call here is potentially slow: connecting, running a
query, fetching rows. All of it therefore lives on a worker object that the UI
moves onto its own QThread and drives through queued signals - the GUI thread
never blocks on the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from mysql_runner.db.driver import (
    CONNECT_TIMEOUT,
    MAX_ROWS,
    MySQLUnavailable,
    connect_kwargs,
    database_from_use,
    describe_error,
    driver_available,
    import_driver,
)
from mysql_runner.db.sqlsplit import Statement, split_statements

# Re-exported so existing importers of this module keep working; the definitions
# live in driver.py, which carries no Qt dependency.
__all__ = [
    "CONNECT_TIMEOUT",
    "MAX_ROWS",
    "MySQLUnavailable",
    "ConnectionParams",
    "MySQLWorker",
    "QueryOutcome",
    "driver_available",
    "import_driver",
]


@dataclass
class QueryOutcome:
    """The result of running one statement."""

    statement: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0
    duration_ms: float = 0.0
    message: str = ""
    error: str = ""
    vertical: bool = False
    truncated: bool = False

    @property
    def is_result_set(self) -> bool:
        return bool(self.columns)


@dataclass
class ConnectionParams:
    """Everything needed to dial a server, as plain data (thread-safe)."""

    host: str
    port: int
    username: str
    password: str
    database: str = ""

    def to_kwargs(self) -> dict:
        return connect_kwargs(
            self.host, self.port, self.username, self.password, self.database
        )


class MySQLWorker(QObject):
    """Owns a live PyMySQL connection on a background thread."""

    #: Emitted with the server banner once the connection is up.
    connected = pyqtSignal(str)
    #: Emitted with a human-readable reason when connecting fails.
    failed = pyqtSignal(str)
    #: Emitted once per executed statement with a QueryOutcome.
    outcome = pyqtSignal(object)
    #: Emitted after the last statement of a submitted batch.
    batch_finished = pyqtSignal()
    #: Emitted after the connection has been closed.
    closed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._conn = None
        self._database = ""

    # ----- lifecycle ------------------------------------------------------
    @pyqtSlot(object)
    def open_connection(self, params: object) -> None:
        assert isinstance(params, ConnectionParams)
        try:
            pymysql = import_driver()
        except MySQLUnavailable as exc:
            self.failed.emit(str(exc))
            return
        try:
            self._conn = pymysql.connect(**params.to_kwargs())
        except Exception as exc:  # pymysql raises a wide range of errors
            self.failed.emit(describe_error(exc))
            return
        self._database = params.database
        self.connected.emit(self._banner(params))

    def _banner(self, params: ConnectionParams) -> str:
        version = "unknown"
        thread_id = "?"
        try:
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                row = cursor.fetchone()
                if row:
                    version = str(row[0])
            thread_id = str(self._conn.thread_id())
        except Exception:
            pass
        target = f"{params.host}:{params.port}"
        db = self._database or "(none)"
        return (
            f"Connected to {target} as {params.username}.\n"
            f"Server version: {version}   Connection id: {thread_id}   "
            f"Database: {db}"
        )

    @pyqtSlot()
    def close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self.closed.emit()

    # ----- execution ------------------------------------------------------
    @pyqtSlot(str)
    def run_sql(self, sql: str) -> None:
        """Execute every statement in ``sql``, emitting one outcome each."""
        if self._conn is None:
            self.outcome.emit(
                QueryOutcome(statement=sql, error="Not connected.")
            )
            self.batch_finished.emit()
            return
        for statement in split_statements(sql):
            self.outcome.emit(self._execute(statement))
        self.batch_finished.emit()

    def _execute(self, statement: Statement) -> QueryOutcome:
        started = time.perf_counter()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(statement.sql)
                elapsed = (time.perf_counter() - started) * 1000
                if cursor.description:
                    columns = [str(col[0]) for col in cursor.description]
                    rows = cursor.fetchmany(MAX_ROWS)
                    truncated = len(rows) == MAX_ROWS and bool(cursor.fetchone())
                    return QueryOutcome(
                        statement=statement.sql,
                        columns=columns,
                        rows=[tuple(r) for r in rows],
                        rowcount=len(rows),
                        duration_ms=elapsed,
                        vertical=statement.vertical,
                        truncated=truncated,
                    )
                affected = cursor.rowcount
                info = getattr(self._conn, "_result", None)
                message = getattr(info, "message", "") or ""
                self._track_database(statement.sql)
                return QueryOutcome(
                    statement=statement.sql,
                    rowcount=affected,
                    duration_ms=elapsed,
                    message=message.strip(),
                    vertical=statement.vertical,
                )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            return QueryOutcome(
                statement=statement.sql,
                duration_ms=elapsed,
                error=describe_error(exc),
                vertical=statement.vertical,
            )

    def _track_database(self, sql: str) -> None:
        """Remember the current schema so the prompt can show it."""
        database = database_from_use(sql)
        if database:
            self._database = database

    @property
    def database(self) -> str:
        return self._database
