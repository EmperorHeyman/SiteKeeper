"""The link between the MCP server and a running Sitekeeper.

An MCP tool call and a file-manager tab were doing the same job through two
entirely separate machines. The tab hands files to a TransferPool: they become
rows in the queue you can cancel, reorder and throttle, the bytes they replace
go into the shadow-backup journal so "undo replace" can put them back, and the
upload is atomic. The MCP server, being a different process, called
``fs.upload()`` straight through - so anything Claude pushed arrived with no
queue row, no undo, no rate limit and no atomic rename. "I don't see any MCP
usage in the queue" was not a display bug; there was nothing to display.

This is the missing wire. The app listens on loopback; the MCP process submits
work to it and blocks until the queue has finished that work, then reports what
happened. Claude's uploads are then the app's uploads in every sense, because
they *are* the app's uploads - the same pool, the same journal, the same rows.

Why a loopback socket rather than a Windows named pipe: Sitekeeper has a macOS
port, a pipe would need pywin32 (which is not a dependency and would have to be
frozen into both builds), and a socket is in the standard library on both. The
listener binds 127.0.0.1 on a port the OS chooses, and writes that port with a
freshly generated token into the app data directory. A caller has to quote the
token, so another user's process cannot drive your transfers by guessing a port
number - and anything that can read the token can already read the vault
metadata sitting beside it, so this adds no exposure that was not there.

When the app is not running, or has no connected tab for the connection being
asked about, there is nothing to submit to. That is not an error: the MCP
server falls back to transferring by itself, exactly as it used to, and says so
in the result so the difference is never silent.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile

from mysql_runner.paths import app_data_dir

#: How long a client waits for the app to finish the work it submitted. A
#: deploy of a few hundred small files over a slow link genuinely can take
#: minutes; past this the client stops waiting and says what it knows.
DEFAULT_TIMEOUT = 900.0

#: Connecting is either instant or hopeless - the listener is on this machine.
CONNECT_TIMEOUT = 2.0

#: One request or reply may not exceed this. A folder upload names every file,
#: and a malformed sender must not be able to make either side allocate
#: without limit.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class BridgeUnavailable(Exception):
    """No running app to submit to. The caller should do the work itself."""


class BridgeError(Exception):
    """The app was reached and refused, or failed, the request."""


def endpoint_path():
    """Where the running app advertises its port and token."""
    return app_data_dir() / "mcp_bridge.json"


# ----- the app's side of the advertisement ---------------------------------
def publish(port: int) -> str:
    """Announce a listener and return the token callers must quote.

    The token is new on every launch, so an endpoint file left behind by a
    crash cannot authorise anything against the next run.
    """
    token = secrets.token_hex(32)
    path = endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as out:
            json.dump({"port": int(port), "token": token, "pid": os.getpid()}, out)
        os.replace(handle.name, path)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    _restrict(path)
    return token


def withdraw() -> None:
    """Take our own advertisement down, and only our own.

    A second Sitekeeper overwrites the file when it starts, so the first one
    closing must not delete what the second is still listening on - that would
    leave a running app that Claude cannot reach and no way to tell why. The
    pid recorded at publish time is what makes the difference visible.
    """
    path = endpoint_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    if data.get("pid") != os.getpid():
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _restrict(path) -> None:
    """Keep the token to its owner where the platform will say so.

    Windows inherits the user profile's ACL, which is already per-user;
    elsewhere the default mode is not, so it is narrowed explicitly. Neither
    is a substitute for the token itself - it is the thing being protected.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ----- the MCP server's side -------------------------------------------------
def _endpoint() -> tuple[int, str]:
    try:
        data = json.loads(endpoint_path().read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BridgeUnavailable("Sitekeeper is not running.") from exc
    port = data.get("port")
    token = data.get("token")
    if not isinstance(port, int) or not isinstance(token, str) or not token:
        raise BridgeUnavailable("Sitekeeper's bridge details are unreadable.")
    return port, token


def call(request: dict, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send one request to the running app and wait for its answer.

    Raises BridgeUnavailable when there is no app to talk to - the caller is
    expected to carry on by itself - and BridgeError when there is one and it
    said no.
    """
    port, token = _endpoint()
    payload = dict(request)
    payload["token"] = token
    line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(line) > MAX_MESSAGE_BYTES:
        raise BridgeError("That request is too large to hand to the app.")
    try:
        connection = socket.create_connection(
            ("127.0.0.1", port), timeout=CONNECT_TIMEOUT
        )
    except OSError as exc:
        # A stale endpoint file from a crashed run looks exactly like this.
        raise BridgeUnavailable("Sitekeeper is not accepting transfers.") from exc
    try:
        # The wait is for the transfer, not for the connection.
        connection.settimeout(timeout)
        connection.sendall(line)
        reply = _read_line(connection)
    except socket.timeout as exc:
        raise BridgeError(
            "Sitekeeper is still working on it. Watch the transfer queue in "
            "the app for the rest."
        ) from exc
    except OSError as exc:
        raise BridgeUnavailable(f"Lost the connection to Sitekeeper: {exc}") from exc
    finally:
        try:
            connection.close()
        except OSError:
            pass
    if not isinstance(reply, dict):
        raise BridgeError("Sitekeeper sent something unreadable.")
    if not reply.get("ok"):
        message = str(reply.get("error") or "Sitekeeper refused that.")
        # "Unavailable" means the app is there but cannot take this particular
        # job - no tab open on that connection, say - which is a reason to do
        # the work directly, not a reason to report a failure.
        if reply.get("code") == "unavailable":
            raise BridgeUnavailable(message)
        raise BridgeError(message)
    return reply


def _read_line(connection: socket.socket) -> object:
    """Read one newline-terminated JSON message."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_MESSAGE_BYTES:
            raise BridgeError("Sitekeeper's answer was too large to read.")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise BridgeUnavailable("Sitekeeper closed the connection.")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise BridgeError("Sitekeeper sent something unreadable.") from exc
