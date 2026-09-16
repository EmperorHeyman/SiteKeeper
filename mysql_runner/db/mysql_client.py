"""Native SQL connection worker.

The console tab talks to the database directly - MySQL on 3306, SQL Server on
1433 - instead of driving phpMyAdmin or SSMS, so every call here is
potentially slow: connecting, running a query, fetching rows. All of it
therefore lives on a worker object that the UI moves onto its own QThread and
drives through queued signals - the GUI thread never blocks on the network.

Which server it is talking to is decided once, by the ConnectionParams it is
opened with, and answered by an engine object from db/engines.py. Nothing
below that line knows there is more than one dialect.

The module keeps its name, and ``MySQLWorker`` keeps working as an alias, so
the FastAPI sidecar and anything else written against the MySQL-only version
still imports what it always did.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from mysql_runner.db import engines
from mysql_runner.db.driver import (
    CONNECT_TIMEOUT,
    MAX_ROWS,
    MySQLUnavailable,
    driver_available,
    import_driver,
)
from mysql_runner.db.engines import ConnectionParams
from mysql_runner.db.mssql_driver import MSSQLUnavailable
from mysql_runner.db.sqlsplit import Statement

# Re-exported so existing importers of this module keep working; the definitions
# live in driver.py and engines.py, neither of which carries a Qt dependency.
__all__ = [
    "CONNECT_TIMEOUT",
    "MAX_ROWS",
    "MySQLUnavailable",
    "ConnectionParams",
    "MySQLWorker",
    "QueryOutcome",
    "SqlWorker",
    "driver_available",
    "import_driver",
]

#: How many names Tab completion will hold for one connection. A schema with
#: more tables than this exists, and completing from the first few thousand is
#: still better than completing from nothing.
MAX_COMPLETIONS = 4000


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


class SqlWorker(QObject):
    """Owns one live database connection on a background thread."""

    #: Emitted with the server banner once the connection is up.
    connected = pyqtSignal(str)
    #: Emitted with a human-readable reason when connecting fails.
    failed = pyqtSignal(str)
    #: Emitted once per executed statement with a QueryOutcome.
    outcome = pyqtSignal(object)
    #: Emitted after the last statement of a submitted batch.
    batch_finished = pyqtSignal()
    #: Emitted with a list of names Tab completion can offer.
    completions = pyqtSignal(object)
    #: Emitted after the connection has been closed.
    closed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._conn = None
        self._database = ""
        self._engine = engines.engine(engines.MYSQL)

    # ----- lifecycle ------------------------------------------------------
    @pyqtSlot(object)
    def open_connection(self, params: object) -> None:
        assert isinstance(params, ConnectionParams)
        self._engine = engines.engine(params.engine)
        if not self._engine.available():
            self.failed.emit(
                self._engine.missing_message() or "No driver for this server."
            )
            return
        try:
            self._conn = self._engine.connect(params)
        except (MySQLUnavailable, MSSQLUnavailable) as exc:
            # "No driver here" is already a sentence; describe_error would
            # try to read it as something a server said.
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # drivers raise a wide range of errors
            self.failed.emit(self._engine.describe_error(exc))
            return
        self._database = params.database
        self.connected.emit(self._engine.banner(self._conn, params))

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
            self.outcome.emit(QueryOutcome(statement=sql, error="Not connected."))
            self.batch_finished.emit()
            return
        for statement in self._engine.split(sql):
            for _ in range(max(1, statement.repeat)):
                for outcome in self._execute(statement):
                    self.outcome.emit(outcome)
        self.batch_finished.emit()

    def _execute(self, statement: Statement) -> list[QueryOutcome]:
        started = time.perf_counter()
        try:
            results = self._engine.execute(self._conn, statement.sql)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            return [
                QueryOutcome(
                    statement=statement.sql,
                    duration_ms=elapsed,
                    error=self._engine.describe_error(exc),
                    vertical=statement.vertical,
                )
            ]
        self._track_database(statement.sql)
        return [
            QueryOutcome(
                statement=statement.sql,
                columns=result.columns,
                rows=result.rows,
                rowcount=result.rowcount,
                duration_ms=result.duration_ms,
                message=result.message,
                vertical=statement.vertical,
                truncated=result.truncated,
            )
            for result in results
        ]

    # ----- Tab completion -------------------------------------------------
    @pyqtSlot()
    def load_completions(self) -> None:
        """Fetch the names Tab can complete: tables, schemas, keywords.

        Deliberately quiet. A console that could not read its own catalogue -
        an account with no rights to information_schema is ordinary - simply
        completes keywords instead, and says nothing about it.
        """
        names: list[str] = list(self._engine.keywords)
        if self._conn is not None:
            for query in self._engine.completion_sql():
                try:
                    for result in self._engine.execute(
                        self._conn, query, max_rows=MAX_COMPLETIONS
                    ):
                        names.extend(
                            str(row[0]) for row in result.rows if row and row[0]
                        )
                except Exception:
                    continue
        self.completions.emit(sorted(set(names)))

    def _track_database(self, sql: str) -> None:
        """Remember the current schema so the prompt can show it."""
        database = self._engine.database_from_use(sql)
        if database:
            self._database = database

    @property
    def database(self) -> str:
        return self._database


#: The name this worker had when MySQL was the only thing it spoke.
MySQLWorker = SqlWorker
