"""A minimal MCP server loop: newline-delimited JSON-RPC 2.0 over stdio.

This implements the subset of the Model Context Protocol that every client
uses to drive a tool server - ``initialize``, ``ping``, ``tools/list`` and
``tools/call`` - with nothing but the standard library. A dependency-free
hundred lines beats freezing the reference SDK and its tail of requirements
into the installer, and there is no transport subtlety to get wrong: one
JSON message per line in, one per line out.

stdout belongs to the protocol, so anything human-readable goes to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback

from mysql_runner.mcp.tools import TOOLS, AppAccess, ToolError

#: Spoken when the client does not name a protocol revision of its own.
PROTOCOL_DEFAULT = "2025-06-18"
SERVER_INFO = {"name": "sitekeeper", "version": "1.5.2"}


class MCPServer:
    """Serves the Sitekeeper toolset to one MCP client over stdio."""

    def __init__(self, access: AppAccess) -> None:
        self._access = access

    # ----- transport --------------------------------------------------------
    def serve(self) -> None:
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        try:
            for raw in stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue  # not ours to guess at
                reply = self._handle(message)
                if reply is not None:
                    stdout.write(
                        json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
                    )
                    stdout.flush()
        finally:
            self._access.close()

    # ----- dispatch -----------------------------------------------------------
    def _handle(self, message: object) -> dict | None:
        if not isinstance(message, dict):
            return None
        method = str(message.get("method", ""))
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if "id" not in message:
            return None  # a notification expects no reply, whatever it was
        msg_id = message.get("id")
        if method == "initialize":
            return _result(msg_id, {
                "protocolVersion": str(
                    params.get("protocolVersion") or PROTOCOL_DEFAULT
                ),
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": _schemas()})
        if method == "tools/call":
            return _result(msg_id, self._call(params))
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method {method!r}"},
        }

    def _call(self, params: object) -> dict:
        name = str(params.get("name", "")) if isinstance(params, dict) else ""
        arguments = params.get("arguments") or {} if isinstance(params, dict) else {}
        entry = TOOLS.get(name)
        if entry is None:
            return _tool_text(f"No tool called {name!r}.", is_error=True)
        handler = entry[0]
        try:
            return _tool_text(handler(self._access, dict(arguments)))
        except ToolError as exc:
            return _tool_text(str(exc), is_error=True)
        except Exception as exc:  # keep serving; give the model the reason
            print(traceback.format_exc(), file=sys.stderr)
            text = str(exc).strip() or exc.__class__.__name__
            return _tool_text(f"{name} failed: {text}", is_error=True)


def _schemas() -> list[dict]:
    return [
        {"name": name, "description": description, "inputSchema": schema}
        for name, (_handler, description, schema) in TOOLS.items()
    ]


def _result(msg_id: object, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _tool_text(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
