"""The app's end of the MCP bridge: take Claude's work, run it through the queue.

One thread accepts connections and reads requests; the answering happens on the
GUI thread, because what it has to do is find a file-manager tab and submit to
its transfer worker, and neither of those may be touched from anywhere else. So
a request arrives on the socket thread, crosses to the GUI thread as a signal,
and the socket thread waits on an event until whatever it started has finished.
That waiting is the point: the MCP call on the other end is blocking too, and
Claude is owed a real answer - "3 uploaded, 1 failed: permission denied" - and
not "submitted, look elsewhere".

Nothing here decides what Claude may do. That was settled in ``mcp/policy.py``
before the request was sent, and re-deciding it here would put the same
question in two places with two answers. What this decides is only whether
there is somewhere to put the work.
"""

from __future__ import annotations

import hmac
import json
import socket
import threading

from PyQt6.QtCore import QObject, pyqtSignal

from mysql_runner.transfer import bridge


class BridgeRequest:
    """One request from the MCP server, and the reply it is blocking on."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self._done = threading.Event()
        self._reply: dict = {}
        #: Set once a reply has been accepted, so a tab that answers twice -
        #: a queue that finishes after a timeout, say - cannot corrupt one.
        self._lock = threading.Lock()

    @property
    def op(self) -> str:
        return str(self.payload.get("op", ""))

    def finish(self, reply: dict) -> None:
        """Answer the waiting caller. The first answer is the one that counts."""
        with self._lock:
            if self._done.is_set():
                return
            self._reply = dict(reply)
            self._done.set()

    def fail(self, message: str, *, unavailable: bool = False) -> None:
        reply = {"ok": False, "error": message}
        if unavailable:
            reply["code"] = "unavailable"
        self.finish(reply)

    def wait(self, timeout: float) -> dict:
        if self._done.wait(timeout):
            return self._reply
        return {
            "ok": False,
            "error": (
                "Sitekeeper did not finish in time. The transfer queue in the "
                "app has the rest of the story."
            ),
        }


class BridgeServer(QObject):
    """Listens on loopback and hands each request to the GUI thread."""

    #: One BridgeRequest, to be answered with finish() or fail().
    request = pyqtSignal(object)
    #: Human-readable notes for the status bar.
    message = pyqtSignal(str)

    #: How long the accepting thread waits for the GUI side to answer before
    #: giving up on one request. Longer than a transfer usually takes and
    #: shorter than the client's own patience, so the client hears a reason
    #: rather than a closed socket.
    ANSWER_TIMEOUT = bridge.DEFAULT_TIMEOUT - 30.0

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._token = ""
        self._stop = threading.Event()

    # ----- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Begin listening. False means the app simply goes without a bridge."""
        if self._thread is not None:
            return True
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            # So the accept loop can notice a stop rather than blocking on a
            # connection that is never going to come.
            listener.settimeout(0.5)
            self._token = bridge.publish(listener.getsockname()[1])
        except OSError as exc:
            self.message.emit(
                f"Claude cannot hand transfers to this window: {exc}"
            )
            return False
        self._socket = listener
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="mcp-bridge", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        bridge.withdraw()
        listener, self._socket = self._socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(2.0)

    # ----- the accept loop ---------------------------------------------------
    def _serve(self) -> None:
        listener = self._socket
        while not self._stop.is_set() and listener is not None:
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return  # closed under us by stop()
            # One connection is one request; a thread each keeps a long upload
            # from holding up a listing behind it.
            threading.Thread(
                target=self._handle,
                args=(connection,),
                name="mcp-bridge-call",
                daemon=True,
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(bridge.CONNECT_TIMEOUT)
            payload = self._read(connection)
            if payload is None:
                return
            # Compared in constant time: the token is the whole of the
            # authorisation, and a timing oracle on it is free to build.
            if not hmac.compare_digest(
                str(payload.get("token", "")), self._token
            ):
                self._write(connection, {"ok": False, "error": "Bad token."})
                return
            payload.pop("token", None)
            pending = BridgeRequest(payload)
            self.request.emit(pending)
            self._write(connection, pending.wait(self.ANSWER_TIMEOUT))
        except OSError:
            pass  # the caller went away; nothing to report to
        finally:
            try:
                connection.close()
            except OSError:
                pass

    @staticmethod
    def _read(connection: socket.socket) -> dict | None:
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                chunk = connection.recv(65536)
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > bridge.MAX_MESSAGE_BYTES:
                return None
            if b"\n" in chunk:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write(connection: socket.socket, reply: dict) -> None:
        try:
            connection.sendall(
                json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
            )
        except OSError:
            pass
