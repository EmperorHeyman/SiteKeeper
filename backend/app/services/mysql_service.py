"""Native MySQL sessions for the console, without Qt.

The Qt build drove PyMySQL from a QObject worker on a QThread. The backend
needs the same behaviour reachable over HTTP, so this keeps a dictionary of open
sessions, each guarded by its own lock, and runs statements on the calling
request thread (uvicorn already gives every request a thread from the pool).

Statement splitting and result rendering are reused verbatim from the library
the Qt build was tested against - see mysql_runner/db/.
"""

from __future__ import annotations

import threading
import time
import uuid

from mysql_runner.db.driver import (
    MAX_ROWS,
    connect_kwargs,
    database_from_use,
    describe_error,
    import_driver,
)
from mysql_runner.db.resultformat import (
    format_summary,
    format_table,
    format_vertical,
    render_value,
)
from mysql_runner.db.sqlsplit import Statement, split_statements
from mysql_runner.storage.models import ServerProfile


class SessionNotFound(KeyError):
    """Raised when a session id does not name an open connection."""


class MySQLSession:
    """One live PyMySQL connection plus the state the console needs."""

    def __init__(self, profile: ServerProfile) -> None:
        self.id = uuid.uuid4().hex
        self.profile_id = profile.id
        self.label = profile.label
        self.target = profile.describe_target()
        self.database = profile.database
        self.opened_at = time.time()
        self._lock = threading.Lock()
        self._conn = None
        self._server_version = "unknown"
        self._thread_id: int | None = None

    # ----- lifecycle ------------------------------------------------------
    def open(self, profile: ServerProfile) -> dict:
        pymysql = import_driver()
        self._conn = pymysql.connect(
            **connect_kwargs(
                profile.host,
                profile.effective_port,
                profile.username,
                profile.password,
                profile.database,
            )
        )
        try:
            with self._conn.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                row = cursor.fetchone()
                if row:
                    self._server_version = str(row[0])
            self._thread_id = self._conn.thread_id()
        except Exception:
            # A banner is cosmetic; never fail the connection over it.
            pass
        return self.info()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def info(self) -> dict:
        return {
            "session_id": self.id,
            "profile_id": self.profile_id,
            "label": self.label,
            "target": self.target,
            "database": self.database,
            "server_version": self._server_version,
            "connection_id": self._thread_id,
            "opened_at": self.opened_at,
        }

    # ----- execution ------------------------------------------------------
    def run(self, sql: str) -> list[dict]:
        """Execute every statement in ``sql``, returning one result each."""
        if self._conn is None:
            return [
                {
                    "statement": sql,
                    "error": "Not connected.",
                    "text": "Not connected.",
                }
            ]
        results: list[dict] = []
        with self._lock:
            for statement in split_statements(sql):
                results.append(self._execute(statement))
        return results

    def _execute(self, statement: Statement) -> dict:
        started = time.perf_counter()
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(statement.sql)
                elapsed = (time.perf_counter() - started) * 1000
                if cursor.description:
                    return self._result_set(cursor, statement, elapsed)
                affected = cursor.rowcount
                self._track_database(statement.sql)
                summary = format_summary(affected, elapsed, False)
                return {
                    "statement": statement.sql,
                    "columns": [],
                    "rows": [],
                    "rowcount": affected,
                    "duration_ms": elapsed,
                    "truncated": False,
                    "vertical": statement.vertical,
                    "text": summary,
                    "summary": summary,
                    "database": self.database,
                }
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            message = describe_error(exc)
            return {
                "statement": statement.sql,
                "columns": [],
                "rows": [],
                "rowcount": 0,
                "duration_ms": elapsed,
                "truncated": False,
                "vertical": statement.vertical,
                "error": message,
                "text": message,
            }

    def _result_set(self, cursor, statement: Statement, elapsed: float) -> dict:
        columns = [str(col[0]) for col in cursor.description]
        raw = cursor.fetchmany(MAX_ROWS)
        truncated = len(raw) == MAX_ROWS and bool(cursor.fetchone())
        tuples = [tuple(r) for r in raw]
        body = (
            format_vertical(columns, tuples)
            if statement.vertical
            else format_table(columns, tuples)
        )
        summary = format_summary(len(tuples), elapsed, True)
        return {
            "statement": statement.sql,
            "columns": columns,
            # Rendered strings, so the UI can show a grid without re-deriving
            # MySQL's own formatting for dates, NULL, TIME and binary columns.
            "rows": [[render_value(value) for value in row] for row in tuples],
            "rowcount": len(tuples),
            "duration_ms": elapsed,
            "truncated": truncated,
            "vertical": statement.vertical,
            "text": f"{body}\n{summary}" if body else summary,
            "summary": summary,
            "database": self.database,
        }

    def _track_database(self, sql: str) -> None:
        database = database_from_use(sql)
        if database:
            self.database = database


class MySQLSessionManager:
    """Owns every open console session in the process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, MySQLSession] = {}

    def open(self, profile: ServerProfile) -> dict:
        session = MySQLSession(profile)
        info = session.open(profile)
        with self._lock:
            self._sessions[session.id] = session
        return info

    def get(self, session_id: str) -> MySQLSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionNotFound(session_id)
        session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list(self) -> list[dict]:
        with self._lock:
            return [s.info() for s in self._sessions.values()]


manager = MySQLSessionManager()
