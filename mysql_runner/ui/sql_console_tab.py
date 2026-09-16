"""In-app SQL command-line console.

A native connection to the database - MySQL on 3306, Microsoft SQL Server on
1433 - driven from a prompt, with the mysql client's look: an ASCII-table
transcript, multi-line statement buffering, a history you can walk with the
arrow keys, and the familiar backslash commands. The connection itself lives
on a worker thread (see db/mysql_client.py), so a slow query never freezes the
window.

Which dialect is being typed is settled once, by the profile's kind, and
answered by an engine from db/engines.py: the prompt, the statement
terminator, the help text and what Tab completes all come from there.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.db import engines
from mysql_runner.db.mysql_client import (
    ConnectionParams,
    QueryOutcome,
    SqlWorker,
)
from mysql_runner.db.resultformat import (
    format_summary,
    format_table,
    format_vertical,
)
from mysql_runner.storage.models import Environment, ServerProfile
from mysql_runner.ui import theme, threadwatch

#: Keep the transcript bounded so a runaway SELECT cannot exhaust memory.
_MAX_BLOCKS = 20_000

#: Beyond this many matches, Tab lists them rather than filling the line.
_COMPLETION_LIST_LIMIT = 60

_HELP_HEAD = """\
Commands (a statement can also span several lines)

  \\?  \\h  help    show this help
  \\c              clear the statement being typed
  \\s              connection status
  \\r              reconnect
  \\q  exit  quit  disconnect this console
  clear  cls      clear the screen"""

_HELP_TAIL = """\

Up / Down walks the history. Tab completes table names and keywords.
Ctrl+L clears the screen."""


def token_at(text: str, position: int) -> tuple[str, int]:
    """The word being completed, and where in ``text`` it starts.

    Broken on whitespace and on the punctuation that separates a name from
    what surrounds it, so Tab after "SELECT * FROM " or "(" completes the
    name rather than the whole line. The dot is deliberately kept: in SQL
    Server a table is ``dbo.Orders`` and that is one name.
    """
    position = max(0, min(position, len(text)))
    start = position
    while start > 0 and text[start - 1] not in " \t\n,;()=<>+-*/'\"`[]":
        start -= 1
    return text[start:position], start


def common_prefix(values: list[str]) -> str:
    """The longest start every value shares, compared case-insensitively."""
    if not values:
        return ""
    shortest = min(values, key=len)
    for index, char in enumerate(shortest):
        if any(value[index].casefold() != char.casefold() for value in values):
            return shortest[:index]
    return shortest


class _PromptEdit(QLineEdit):
    """Single-line input that walks the history and completes with Tab."""

    history_back = pyqtSignal()
    history_forward = pyqtSignal()
    #: The line as typed and where the cursor is, for Tab completion.
    completion_requested = pyqtSignal(str, int)

    def event(self, event) -> bool:  # noqa: N802 - Qt naming
        """Take Tab before Qt hands it to the focus chain.

        Qt answers Tab in QWidget::event, moving focus to the next widget,
        and only calls keyPressEvent for keys it did not claim. So a Tab
        handler in keyPressEvent - which is where this one used to be - never
        runs: pressing Tab in a console jumped to the Disconnect button
        instead of completing anything, which is exactly what it must not do.
        """
        if event.type() == QEvent.Type.KeyPress and event.key() in (
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        ):
            self.completion_requested.emit(self.text(), self.cursorPosition())
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Up:
            self.history_back.emit()
            return
        if event.key() == Qt.Key.Key_Down:
            self.history_forward.emit()
            return
        super().keyPressEvent(event)


class SqlConsoleTab(QWidget):
    """A database shell in a tab: MySQL or SQL Server."""

    status_message = pyqtSignal(str)
    title_changed = pyqtSignal(str)

    # Requests handed to the worker thread.
    _open_requested = pyqtSignal(object)
    _sql_requested = pyqtSignal(str)
    _completions_requested = pyqtSignal()
    _close_requested = pyqtSignal()

    def __init__(
        self,
        profile: ServerProfile,
        parent: QWidget | None = None,
        *,
        dark_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._engine = engines.engine_for(profile.kind)
        self._pending: list[str] = []      # Lines of a half-typed statement.
        self._history: list[str] = []
        self._history_index = 0
        self._busy = False
        self._connected = False
        #: Why the last attempt to connect failed, for whoever asked for
        #: this tab to be opened and is waiting to hear.
        self._last_error = ""
        #: Names Tab can offer: keywords, then whatever the catalogue had.
        self._names: list[str] = list(self._engine.keywords)
        #: A statement the MCP bridge is waiting on, and the outcomes it has
        #: produced so far. One at a time: see accept_bridge_query.
        self._bridge_query: dict | None = None
        self._closing = False
        self._startup_done = False

        self._build_ui()
        self.set_dark_mode(dark_mode)
        self._start_worker()
        self._connect_to_server()

    # ----- UI -------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setMaximumBlockCount(_MAX_BLOCKS)
        self._transcript.setFont(_mono_font())
        self._transcript.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._transcript, 1)

        prompt_row = QHBoxLayout()
        if self._profile.environment == Environment.PROD:
            # Beside the prompt rather than above the transcript: this is the
            # spot you look at while typing the statement it is warning about.
            prompt_row.addWidget(
                theme.production_badge(
                    "Statements run against the live database."
                )
            )
        self._prompt_label = QLabel(self._engine.prompt.strip())
        self._prompt_label.setObjectName("prompt")
        self._prompt_label.setFont(_mono_font())
        self._input = _PromptEdit()
        self._input.setFont(_mono_font())
        self._input.setPlaceholderText("Connecting…")
        self._input.setEnabled(False)
        self._input.returnPressed.connect(self._on_submit)
        self._input.history_back.connect(lambda: self._walk_history(-1))
        self._input.history_forward.connect(lambda: self._walk_history(1))
        self._input.completion_requested.connect(self._on_complete)
        self._cancel_btn = QPushButton("Disconnect")
        self._cancel_btn.clicked.connect(self._on_disconnect_clicked)
        prompt_row.addWidget(self._prompt_label)
        prompt_row.addWidget(self._input, 1)
        prompt_row.addWidget(self._cancel_btn)
        layout.addLayout(prompt_row)

        clear = QShortcut(QKeySequence("Ctrl+L"), self)
        clear.activated.connect(self._clear_screen)

    def set_dark_mode(self, enable: bool) -> None:
        self.setStyleSheet(theme.console_stylesheet(enable))

    def current_title(self) -> str:
        return f"{self._profile.label} - SQL"

    @property
    def engine(self):
        """Which dialect this console speaks. Read by the MCP bridge."""
        return self._engine

    @property
    def is_connected(self) -> bool:
        """Whether there is a live connection behind this tab."""
        return self._connected

    @property
    def last_error(self) -> str:
        """Why the last connection attempt failed, if one did."""
        return self._last_error

    @property
    def server_profile(self) -> ServerProfile:
        return self._profile

    # ----- worker wiring --------------------------------------------------
    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = SqlWorker()
        self._worker.moveToThread(self._thread)

        self._open_requested.connect(self._worker.open_connection)
        self._sql_requested.connect(self._worker.run_sql)
        self._completions_requested.connect(self._worker.load_completions)
        self._close_requested.connect(self._worker.close_connection)

        self._worker.connected.connect(self._on_connected)
        self._worker.failed.connect(self._on_failed)
        self._worker.outcome.connect(self._on_outcome)
        self._worker.batch_finished.connect(self._on_batch_finished)
        self._worker.completions.connect(self._on_completions)
        self._worker.closed.connect(self._on_closed)

        self._thread.start()

    def _connect_to_server(self) -> None:
        profile = self._profile
        self._write(f"Connecting to {profile.describe_target()} …")
        self._open_requested.emit(ConnectionParams.from_profile(profile))

    # ----- worker callbacks -----------------------------------------------
    def _on_connected(self, banner: str) -> None:
        self._connected = True
        self._last_error = ""
        self._write(banner)
        self._write("Type \\? for help.\n")
        self._input.setEnabled(True)
        self._input.setPlaceholderText("")
        self._input.setFocus()
        self.status_message.emit(f"Connected to {self._profile.label}")
        self.title_changed.emit(self.current_title())
        # Ask for the catalogue before anything else is queued, so Tab works
        # from the first keystroke rather than after the first query.
        self._completions_requested.emit()
        self._run_startup_script()

    def _on_completions(self, names: object) -> None:
        self._names = [str(name) for name in (names or [])]

    def _on_failed(self, message: str) -> None:
        self._connected = False
        self._last_error = message
        self._write(message)
        self._write("")
        self._input.setEnabled(False)
        self._input.setPlaceholderText("Not connected - press Reconnect")
        self._cancel_btn.setText("Reconnect")
        self.status_message.emit(f"{self._profile.label}: {message}")

    def _on_outcome(self, outcome: object) -> None:
        assert isinstance(outcome, QueryOutcome)
        if self._bridge_query is not None:
            self._bridge_query["outcomes"].append(outcome)
        if outcome.error:
            self._write(outcome.error)
            self._write("")
            return
        if outcome.is_result_set:
            body = (
                format_vertical(outcome.columns, outcome.rows)
                if outcome.vertical
                else format_table(outcome.columns, outcome.rows)
            )
            if body:
                self._write(body)
        self._write(
            format_summary(
                outcome.rowcount, outcome.duration_ms, outcome.is_result_set
            )
        )
        if outcome.truncated:
            self._write(
                f"(output limited to the first {outcome.rowcount} rows - "
                "add a LIMIT clause to see a specific slice)"
            )
        if outcome.message:
            self._write(outcome.message)
        self._write("")

    def _on_batch_finished(self) -> None:
        self._set_busy(False)
        self._finish_bridge_query()

    # ----- statements handed over by the MCP bridge -----------------------
    # Claude used to open its own MySQL connection in the MCP process, so a
    # query it ran left no trace anywhere in this window. Running it here
    # instead puts the statement and its output in the transcript you are
    # already looking at, on the connection this tab already holds - and marks
    # it as Claude's, because an unexplained statement appearing in your own
    # console would be worse than not seeing it at all.
    def accept_bridge_query(self, sql: str, on_done) -> str:
        """Run one batch for the bridge. Returns "" if it was taken."""
        if not self._connected:
            return "the SQL console for that connection is not connected"
        if self._busy or self._bridge_query is not None:
            return "that SQL console is in the middle of something"
        if not sql.strip():
            return "no SQL was given"
        self._bridge_query = {"on_done": on_done, "outcomes": []}
        self._write(
            self._engine.prompt + "-- run by Claude", newline_before=True
        )
        for line in sql.strip().splitlines():
            self._write(self._engine.continuation + line, newline_before=False)
        self._set_busy(True)
        self._sql_requested.emit(sql)
        return ""

    def _finish_bridge_query(self, abandoned: str = "") -> None:
        """Hand the collected output back to whoever asked for it."""
        pending, self._bridge_query = self._bridge_query, None
        if pending is None:
            return
        callback = pending["on_done"]
        if abandoned:
            callback({"ok": False, "error": abandoned})
            return
        callback(
            {
                "ok": True,
                "detail": _render(pending["outcomes"], self._engine.prompt),
                "engine": self._engine.key,
            }
        )

    def _abandon_bridge_query(self, reason: str) -> None:
        self._finish_bridge_query(abandoned=reason)

    def _on_closed(self) -> None:
        self._connected = False
        self._abandon_bridge_query("the connection closed before it finished")
        self._input.setEnabled(False)
        self._input.setPlaceholderText("Disconnected - press Reconnect")
        self._cancel_btn.setText("Reconnect")

    # ----- input handling -------------------------------------------------
    def _on_submit(self) -> None:
        line = self._input.text()
        self._input.clear()
        stripped = line.strip()

        if stripped:
            self._history.append(line)
            self._history_index = len(self._history)

        # Echo what was typed, at the prompt that was showing.
        self._write(
            (self._engine.continuation if self._pending else self._engine.prompt)
            + line,
            newline_before=False,
        )

        # Backslash commands work anywhere - cancelling a half-typed statement
        # with \c is the whole point of it. The word aliases (help, exit, clear)
        # only count at the start, so they can still appear inside a statement.
        at_start = not self._pending
        if (at_start or stripped.startswith("\\")) and self._handle_meta(stripped):
            return

        self._pending.append(line)
        buffered = "\n".join(self._pending)
        if not self._engine.is_complete(buffered):
            self._prompt_label.setText(self._engine.continuation.strip())
            return

        self._pending = []
        self._prompt_label.setText(self._engine.prompt.strip())
        if not self._connected:
            self._write("Not connected.\n")
            return
        self._set_busy(True)
        self._sql_requested.emit(buffered)

    def _handle_meta(self, text: str) -> bool:
        """Handle a client-side command. Returns True when it was one."""
        lowered = text.lower()
        if lowered in ("\\q", "exit", "quit"):
            self._write("Bye")
            self._close_requested.emit()
            return True
        if lowered in ("\\?", "\\h", "help"):
            self._write(self._help_text() + "\n")
            return True
        if lowered == "\\c":
            self._pending = []
            self._prompt_label.setText(self._engine.prompt.strip())
            return True
        if lowered == "\\s":
            self._write(self._status_text() + "\n")
            return True
        if lowered == "\\r":
            self._reconnect()
            return True
        if lowered in ("clear", "cls"):
            self._clear_screen()
            return True
        return False

    def _help_text(self) -> str:
        """The help, with the bits that differ between dialects filled in."""
        lines = [_HELP_HEAD]
        if not self._engine.tsql:
            lines.append("  <statement>\\G   run and print each row vertically")
        lines.append("")
        lines.append(f"  {self._engine.name}: {self._engine.terminator_help}.")
        lines.append(_HELP_TAIL)
        return "\n".join(lines)

    def _status_text(self) -> str:
        state = "connected" if self._connected else "not connected"
        return (
            f"Connection: {self._profile.describe_target()} ({state})\n"
            f"Profile:    {self._profile.label}\n"
            f"Server:     {self._engine.name}\n"
            f"Environment: {self._profile.environment.value}"
        )

    # ----- completion -----------------------------------------------------
    # Tab used to move focus to the Disconnect button, which in a console is
    # not a neutral thing to do: it is the key you press when you cannot
    # remember the name of a table, and it took you away from the line you
    # were typing. It now completes against the keywords of this dialect and
    # the catalogue read when the connection opened.
    def _on_complete(self, text: str, position: int) -> None:
        token, start = token_at(text, position)
        if not token:
            return
        needle = token.casefold()
        matches = [name for name in self._names if name.casefold().startswith(needle)]
        if not matches:
            return
        if len(matches) == 1:
            self._fill(start, token, matches[0], finished=True)
            return
        shared = common_prefix(matches)
        if len(shared) > len(token):
            self._fill(start, token, shared, finished=False)
            return
        # Nothing more to fill in: show the choices the way a shell does,
        # rather than silently doing nothing and looking broken.
        shown = matches[:_COMPLETION_LIST_LIMIT]
        self._write("   ".join(shown), newline_before=True)
        if len(matches) > len(shown):
            self._write(f"… and {len(matches) - len(shown)} more")
        self._write("")

    def _fill(self, start: int, token: str, value: str, *, finished: bool) -> None:
        """Replace the word being completed with ``value``."""
        text = self._input.text()
        end = start + len(token)
        tail = " " if finished else ""
        self._input.setText(text[:start] + value + tail + text[end:])
        self._input.setCursorPosition(start + len(value) + len(tail))

    def _walk_history(self, delta: int) -> None:
        if not self._history:
            return
        self._history_index = max(
            0, min(len(self._history), self._history_index + delta)
        )
        if self._history_index == len(self._history):
            self._input.clear()
        else:
            self._input.setText(self._history[self._history_index])
        self._input.end(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._input.setEnabled(self._connected and not busy)
        if busy:
            self.status_message.emit(f"Running on {self._profile.label}…")
        else:
            self._input.setFocus()

    def _run_startup_script(self) -> None:
        script = self._profile.startup_script.strip()
        if self._startup_done or not script:
            return
        self._startup_done = True
        self._write(
            self._engine.prompt + script.replace("\n", " "), newline_before=False
        )
        self._set_busy(True)
        self._sql_requested.emit(script)

    # ----- actions --------------------------------------------------------
    def _on_disconnect_clicked(self) -> None:
        if self._connected:
            self._write("Bye\n")
            self._close_requested.emit()
        else:
            self._reconnect()

    def _reconnect(self) -> None:
        self._cancel_btn.setText("Disconnect")
        self._startup_done = False
        self._connect_to_server()

    def _clear_screen(self) -> None:
        self._transcript.clear()

    # ----- transcript -----------------------------------------------------
    def _write(self, text: str, *, newline_before: bool = False) -> None:
        if newline_before:
            self._transcript.appendPlainText("")
        self._transcript.appendPlainText(text)
        self._transcript.moveCursor(QTextCursor.MoveOperation.End)

    # ----- teardown -------------------------------------------------------
    def cleanup(self) -> None:
        """Close the connection and stop the worker thread."""
        self._closing = True
        self._abandon_bridge_query("the tab was closed before it finished")
        # Detach first: a connect or query still running must not deliver its
        # result into a widget that is being destroyed.
        try:
            self._worker.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self._close_requested.emit()
        except RuntimeError:
            pass
        self._thread.quit()
        # Give the worker a moment to unwind; it only has to close a socket.
        # A connect that is still in flight cannot be interrupted, though,
        # and destroying the thread under it aborts the process - so one
        # that misses the deadline is retired rather than destroyed.
        if not self._thread.wait(3000):
            threadwatch.retire(self._thread, self._worker)


def _render(outcomes, prompt: str = "mysql> ") -> str:
    """The same output the transcript shows, as text for the caller.

    Deliberately the mysql-client shape the MCP server already returns for
    this tool, so an answer that came through the app and one that did not
    read identically - the difference is where it happened, not what it says.
    """
    blocks = []
    for outcome in outcomes:
        if outcome.error:
            blocks.append(f"{prompt}{outcome.statement.strip()}\n{outcome.error}")
            continue
        body = ""
        if outcome.is_result_set:
            body = (
                format_vertical(outcome.columns, outcome.rows)
                if outcome.vertical
                else format_table(outcome.columns, outcome.rows)
            )
            body += "\n" if body else ""
        body += format_summary(
            outcome.rowcount, outcome.duration_ms, outcome.is_result_set
        )
        if outcome.truncated:
            body += f"\n(only the first {outcome.rowcount} rows are shown)"
        if outcome.message:
            body += f"\n{outcome.message}"
        blocks.append(f"{prompt}{outcome.statement.strip()}\n{body}")
    return "\n\n".join(blocks) or "No output."


def _mono_font() -> QFont:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font
