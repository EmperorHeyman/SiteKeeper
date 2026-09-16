"""One seam between "a SQL console" and "which SQL server it is talking to".

Before SQL Server existed here, the console, the sidecar and the MCP server
each opened PyMySQL themselves and each rendered the results their own way.
Adding a second dialect that way would have meant three copies of the same
if/else, drifting apart the moment one of them gained a fix - which is what
had already happened to the execution loop.

So the differences live here, in two small engine objects, and everything
above asks the engine rather than asking the profile's kind:

    engine = engine_for(profile.kind)
    params = ConnectionParams.from_profile(profile)
    conn = engine.connect(params)
    for result in engine.execute(conn, "SELECT 1"):
        ...

Nothing in this module imports Qt, and neither driver is imported until
something actually connects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from mysql_runner.db import driver as mysqldriver
from mysql_runner.db import mssql_driver
from mysql_runner.db.sqlsplit import Statement, split_statements, is_complete

#: Engine keys, as stored in ConnectionParams and used by the registry. They
#: match the ConnectionKind values, but this module does not depend on that.
MYSQL = "mysql"
MSSQL = "mssql"


@dataclass
class RawResult:
    """One result set (or one row count) produced by a statement."""

    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0
    duration_ms: float = 0.0
    message: str = ""
    truncated: bool = False

    @property
    def is_result_set(self) -> bool:
        return bool(self.columns)


@dataclass
class ConnectionParams:
    """Everything needed to dial a server, as plain data (thread-safe).

    Plain data because the Qt console hands this across a thread boundary to
    a worker, and a ServerProfile carries more than a connection needs.
    """

    host: str
    port: int
    username: str
    password: str
    database: str = ""
    #: Which engine dials it: MYSQL or MSSQL.
    engine: str = MYSQL
    # ----- SQL Server only ------------------------------------------------
    instance: str = ""
    windows_auth: bool = False
    encrypt: bool = True
    trust_certificate: bool = True
    odbc_driver: str = ""

    @classmethod
    def from_profile(cls, profile, *, database: str | None = None) -> "ConnectionParams":
        """The connection a stored profile describes.

        ``database`` overrides the profile's own, which is what a caller that
        was told which schema to use passes.
        """
        return cls(
            host=profile.host,
            port=profile.effective_port,
            username=profile.username,
            password=profile.password,
            database=profile.database if database is None else database,
            engine=MSSQL if profile.kind.value == MSSQL else MYSQL,
            instance=getattr(profile, "mssql_instance", ""),
            windows_auth=bool(getattr(profile, "mssql_windows_auth", False)),
            encrypt=bool(getattr(profile, "mssql_encrypt", True)),
            trust_certificate=bool(getattr(profile, "mssql_trust_cert", True)),
            odbc_driver=getattr(profile, "mssql_odbc_driver", ""),
        )

    def to_kwargs(self) -> dict:
        """MySQL connect kwargs. Kept for callers written before engines."""
        return mysqldriver.connect_kwargs(
            self.host, self.port, self.username, self.password, self.database
        )


class Engine:
    """What a SQL console needs to know about one kind of server."""

    key = MYSQL
    #: How it is named in a sentence: "Connected to ... (MySQL)".
    name = "MySQL"
    #: The console's prompt, and the one shown while a statement is unfinished.
    prompt = "mysql> "
    continuation = "    -> "
    #: One line saying how to end a statement, for the console's help.
    terminator_help = "end a statement with ; (or \\G for one row per line)"
    #: Whether statements are T-SQL rather than MySQL.
    tsql = False

    # ----- availability ---------------------------------------------------
    def available(self) -> bool:
        raise NotImplementedError

    def missing_message(self) -> str:
        raise NotImplementedError

    # ----- statements -----------------------------------------------------
    def split(self, text: str) -> list[Statement]:
        return split_statements(text, tsql=self.tsql)

    def is_complete(self, text: str) -> bool:
        return is_complete(text, tsql=self.tsql)

    def database_from_use(self, sql: str) -> str | None:
        raise NotImplementedError

    # ----- connecting -----------------------------------------------------
    def connect(self, params: ConnectionParams):
        raise NotImplementedError

    def describe_error(self, exc: Exception) -> str:
        raise NotImplementedError

    def server_facts(self, conn) -> tuple[str, str, str]:
        """(version, connection id, current database), for a status line.

        All three are cosmetic, and asking for them must never fail a
        connection that otherwise works - an account with no rights to a
        system view is an ordinary thing, not a reason to refuse it a
        console. Anything that cannot be read comes back empty.
        """
        raise NotImplementedError

    def banner(self, conn, params: ConnectionParams) -> str:
        raise NotImplementedError

    # ----- running --------------------------------------------------------
    def execute(self, conn, sql: str, *, max_rows: int = 0) -> list[RawResult]:
        """Run one statement and collect everything it produced.

        A list rather than a single result because one T-SQL batch can return
        several result sets, and showing only the first would silently lose
        the rest.
        """
        raise NotImplementedError

    def completion_sql(self) -> list[str]:
        """Queries whose first column names things worth completing."""
        return []

    #: Words offered by Tab completion in a console on this engine.
    keywords: tuple[str, ...] = ()


class MySqlEngine(Engine):
    key = MYSQL
    name = "MySQL"
    prompt = "mysql> "
    continuation = "    -> "
    terminator_help = "end a statement with ; (or \\G for one row per line)"
    tsql = False

    keywords = (
        "ALTER", "ANALYZE", "AND", "AS", "ASC", "BETWEEN", "BY", "CALL",
        "CASE", "CHANGE", "CHARACTER", "COLLATE", "COLUMN", "COMMIT",
        "CREATE", "CROSS", "DATABASE", "DATABASES", "DEFAULT", "DELETE",
        "DESC", "DESCRIBE", "DISTINCT", "DROP", "ELSE", "END", "ENGINE",
        "EXISTS", "EXPLAIN", "FROM", "FULL", "GRANT", "GROUP", "HAVING",
        "IGNORE", "INDEX", "INNER", "INSERT", "INTO", "IS", "JOIN", "KEY",
        "LEFT", "LIKE", "LIMIT", "LOCK", "NOT", "NULL", "OFFSET", "ON",
        "OPTIMIZE", "OR", "ORDER", "OUTER", "PRIMARY", "PROCESSLIST",
        "REPLACE", "RIGHT", "ROLLBACK", "SCHEMA", "SELECT", "SET", "SHOW",
        "START", "STATUS", "TABLE", "TABLES", "THEN", "TRANSACTION",
        "TRUNCATE", "UNION", "UNIQUE", "UPDATE", "USE", "VALUES", "VARIABLES",
        "VIEW", "WHEN", "WHERE", "WITH",
    )

    def available(self) -> bool:
        return mysqldriver.driver_available()

    def missing_message(self) -> str:
        if self.available():
            return ""
        return (
            "This build has no MySQL driver (PyMySQL), so SQL console tabs "
            "cannot connect. Install PyMySQL and restart."
        )

    def database_from_use(self, sql: str) -> str | None:
        return mysqldriver.database_from_use(sql)

    def connect(self, params: ConnectionParams):
        pymysql = mysqldriver.import_driver()
        return pymysql.connect(**params.to_kwargs())

    def describe_error(self, exc: Exception) -> str:
        return mysqldriver.describe_error(exc)

    def server_facts(self, conn) -> tuple[str, str, str]:
        version = ""
        thread_id = ""
        database = ""
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION(), DATABASE()")
                row = cursor.fetchone()
                if row:
                    version = str(row[0] or "")
                    database = str(row[1] or "")
            thread_id = str(conn.thread_id())
        except Exception:
            pass  # cosmetic; never fail a connection over it
        return version, thread_id, database

    def banner(self, conn, params: ConnectionParams) -> str:
        version, thread_id, database = self.server_facts(conn)
        return (
            f"Connected to {params.host}:{params.port} as {params.username}.\n"
            f"Server version: {version or 'unknown'}   "
            f"Connection id: {thread_id or '?'}   "
            f"Database: {database or params.database or '(none)'}"
        )

    def execute(self, conn, sql: str, *, max_rows: int = 0) -> list[RawResult]:
        limit = max_rows or mysqldriver.MAX_ROWS
        started = time.perf_counter()
        with conn.cursor() as cursor:
            cursor.execute(sql)
            elapsed = (time.perf_counter() - started) * 1000
            if cursor.description:
                columns = [str(col[0]) for col in cursor.description]
                rows = cursor.fetchmany(limit)
                truncated = len(rows) == limit and bool(cursor.fetchone())
                return [
                    RawResult(
                        columns=columns,
                        rows=[tuple(row) for row in rows],
                        rowcount=len(rows),
                        duration_ms=elapsed,
                        truncated=truncated,
                    )
                ]
            info = getattr(conn, "_result", None)
            return [
                RawResult(
                    rowcount=cursor.rowcount,
                    duration_ms=elapsed,
                    message=(getattr(info, "message", "") or "").strip(),
                )
            ]

    def completion_sql(self) -> list[str]:
        return [
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME LIMIT 2000",
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "ORDER BY SCHEMA_NAME LIMIT 500",
        ]


class SqlServerEngine(Engine):
    key = MSSQL
    name = "SQL Server"
    prompt = "mssql> "
    continuation = "    -> "
    terminator_help = "end a statement with ; or a line containing only GO"
    tsql = True

    keywords = (
        "ALTER", "AND", "AS", "ASC", "BEGIN", "BETWEEN", "BY", "CASE", "CAST",
        "CATCH", "COLUMN", "COMMIT", "CONSTRAINT", "CONVERT", "CREATE",
        "CROSS", "DATABASE", "DECLARE", "DEFAULT", "DELETE", "DESC", "DISTINCT",
        "DROP", "ELSE", "END", "EXEC", "EXECUTE", "EXISTS", "FROM", "FULL",
        "FUNCTION", "GO", "GRANT", "GROUP", "HAVING", "IDENTITY", "IF",
        "INDEX", "INNER", "INSERT", "INTO", "IS", "JOIN", "KEY", "LEFT",
        "LIKE", "MERGE", "NOCOUNT", "NOLOCK", "NOT", "NULL", "OFFSET", "ON",
        "ORDER", "OUTER", "OVER", "PARTITION", "PRIMARY", "PRINT", "PROCEDURE",
        "RIGHT", "ROLLBACK", "ROW_NUMBER", "SCHEMA", "SELECT", "SET", "TABLE",
        "THEN", "TOP", "TRANSACTION", "TRIGGER", "TRUNCATE", "TRY", "UNION",
        "UNIQUE", "UPDATE", "USE", "VALUES", "VIEW", "WHEN", "WHERE", "WHILE",
        "WITH",
    )

    def available(self) -> bool:
        return mssql_driver.driver_available() and bool(
            mssql_driver.installed_drivers()
        )

    def missing_message(self) -> str:
        return mssql_driver.missing_driver_message()

    def database_from_use(self, sql: str) -> str | None:
        return mssql_driver.database_from_use(sql)

    def connect(self, params: ConnectionParams):
        pyodbc = mssql_driver.import_driver()
        return pyodbc.connect(
            mssql_driver.connection_string(
                params.host,
                params.port,
                params.username,
                params.password,
                params.database,
                instance=params.instance,
                windows_auth=params.windows_auth,
                encrypt=params.encrypt,
                trust_certificate=params.trust_certificate,
                odbc_driver=params.odbc_driver,
            ),
            autocommit=True,
            timeout=mssql_driver.CONNECT_TIMEOUT,
        )

    def describe_error(self, exc: Exception) -> str:
        return mssql_driver.describe_error(exc)

    def server_facts(self, conn) -> tuple[str, str, str]:
        version = ""
        spid = ""
        database = ""
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(64)), "
                    "CAST(SERVERPROPERTY('Edition') AS varchar(128)), "
                    "DB_NAME(), @@SPID"
                )
                row = cursor.fetchone()
                if row:
                    version = str(row[0] or "")
                    edition = str(row[1] or "")
                    if edition:
                        version = f"{version} ({edition})".strip()
                    database = str(row[2] or "")
                    spid = str(row[3] or "")
            finally:
                cursor.close()
        except Exception:
            pass  # cosmetic; never fail a connection over it
        return version, spid, database

    def banner(self, conn, params: ConnectionParams) -> str:
        version, spid, database = self.server_facts(conn)
        who = "Windows authentication" if params.windows_auth else params.username
        target = mssql_driver.server_address(
            params.host, params.port, params.instance
        )
        return (
            f"Connected to {target} as {who}.\n"
            f"SQL Server {version or 'unknown'}   "
            f"Session id: {spid or '?'}   "
            f"Database: {database or params.database or '(none)'}"
        )

    def execute(self, conn, sql: str, *, max_rows: int = 0) -> list[RawResult]:
        limit = max_rows or mssql_driver.MAX_ROWS
        results: list[RawResult] = []
        started = time.perf_counter()
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            while True:
                elapsed = (time.perf_counter() - started) * 1000
                if cursor.description:
                    columns = [str(col[0]) for col in cursor.description]
                    rows = cursor.fetchmany(limit)
                    truncated = len(rows) == limit and bool(cursor.fetchone())
                    results.append(
                        RawResult(
                            columns=columns,
                            rows=[tuple(row) for row in rows],
                            rowcount=len(rows),
                            duration_ms=elapsed,
                            truncated=truncated,
                        )
                    )
                else:
                    results.append(
                        RawResult(rowcount=cursor.rowcount, duration_ms=elapsed)
                    )
                if not _next_result_set(cursor):
                    break
        finally:
            try:
                cursor.close()
            except Exception:
                pass
        return results or [RawResult(rowcount=-1)]

    def completion_sql(self) -> list[str]:
        return [
            "SELECT TOP 2000 TABLE_SCHEMA + '.' + TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_NAME",
            "SELECT TOP 500 name FROM sys.databases ORDER BY name",
        ]


def _next_result_set(cursor) -> bool:
    """Move to the next result set, treating "there is none" as False.

    pyodbc returns False when a batch is finished, but some drivers raise
    instead - and a raise here would lose the results already collected.
    """
    try:
        return bool(cursor.nextset())
    except Exception:
        return False


_ENGINES: dict[str, Engine] = {
    MYSQL: MySqlEngine(),
    MSSQL: SqlServerEngine(),
}


def engine(key: str) -> Engine:
    """The engine for a key, defaulting to MySQL as everything used to."""
    return _ENGINES.get(key or MYSQL, _ENGINES[MYSQL])


def engine_for(kind) -> Engine:
    """The engine for a ConnectionKind (or anything with a .value)."""
    return engine(getattr(kind, "value", str(kind)))


