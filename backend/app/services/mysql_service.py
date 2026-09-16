"""Native SQL sessions for the console, without Qt.

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

from mysql_runner.db import engines
from mysql_runner.db.driver import MAX_ROWS
from mysql_runner.db.engines import ConnectionParams, RawResult
from mysql_runner.db.resultformat import (
    format_summary,
    format_table,
    format_vertical,
    render_value,
)
from mysql_runner.db.sqlsplit import Statement
from mysql_runner.storage.models import ServerProfile


class SessionNotFound(KeyError):
    """Raised when a session id does not name an open connection."""


class MySQLSession:
    """One live database connection plus the state the console needs.

    Named for MySQL because that is all it spoke when it was written; the
    dialect now comes from the profile's engine, so the same session type
    serves SQL Server too.
    """

    def __init__(self, profile: ServerProfile) -> None:
        self.id = uuid.uuid4().hex
        self.engine = engines.engine_for(profile.kind)
        self.profile_id = profile.id
        self.label = profile.label
        self.target = profile.describe_target()
        self.database = profile.database
        self.opened_at = time.time()
        self._lock = threading.Lock()
        self._conn = None
        self._server_version = "unknown"
        self._thread_id: str = ""
        self._banner = ""

    # ----- lifecycle ------------------------------------------------------
    def open(self, profile: ServerProfile) -> dict:
        params = ConnectionParams.from_profile(profile)
        self._conn = self.engine.connect(params)
        version, connection_id, database = self.engine.server_facts(self._conn)
        self._server_version = version or "unknown"
        self._thread_id = connection_id
        self.database = database or self.database
        # Sent whole as well, so a client does not have to know that SQL
        # Server calls a connection id a session id.
        self._banner = self.engine.banner(self._conn, params)
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
            "engine": self.engine.key,
            "prompt": self.engine.prompt,
            "banner": self._banner,
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
            for statement in self.engine.split(sql):
                for _ in range(max(1, statement.repeat)):
                    results.extend(self._execute(statement))
        return results

    def _execute(self, statement: Statement) -> list[dict]:
        """Run one statement. A list, because a T-SQL batch can return
        several result sets and showing only the first would lose the rest.
        """
        started = time.perf_counter()
        try:
            raw = self.engine.execute(
                self._conn, statement.sql, max_rows=MAX_ROWS
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            message = self.engine.describe_error(exc)
            return [
                {
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
            ]
        self._track_database(statement.sql)
        return [self._rendered(result, statement) for result in raw]

    def _rendered(self, result: RawResult, statement: Statement) -> dict:
        if not result.is_result_set:
            summary = format_summary(
                result.rowcount, result.duration_ms, False
            )
            return {
                "statement": statement.sql,
                "columns": [],
                "rows": [],
                "rowcount": result.rowcount,
                "duration_ms": result.duration_ms,
                "truncated": False,
                "vertical": statement.vertical,
                "text": summary,
                "summary": summary,
                "database": self.database,
            }
        body = (
            format_vertical(result.columns, result.rows)
            if statement.vertical
            else format_table(result.columns, result.rows)
        )
        summary = format_summary(result.rowcount, result.duration_ms, True)
        return {
            "statement": statement.sql,
            "columns": result.columns,
            # Rendered strings, so the UI can show a grid without re-deriving
            # the client's own formatting for dates, NULL, TIME and binary.
            "rows": [
                [render_value(value) for value in row] for row in result.rows
            ],
            "rowcount": result.rowcount,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
            "vertical": statement.vertical,
            "text": f"{body}\n{summary}" if body else summary,
            "summary": summary,
            "database": self.database,
        }

    def _track_database(self, sql: str) -> None:
        database = self.engine.database_from_use(sql)
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
