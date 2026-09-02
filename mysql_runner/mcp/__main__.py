"""Entry point: ``python -m mysql_runner.mcp`` starts the MCP server.

Register it with Claude Code from anywhere:

    claude mcp add sitekeeper -- python -m mysql_runner.mcp

There is nothing else to put on that line. What Claude may do is decided in
Sitekeeper's "Connect Claude" window and read from the app's own grants file
on every tool call - see ``mcp/policy.py`` for why it moved off the command
line, and ``mcp/tools.py`` for what each grant covers.

The old ``--allow-*`` and ``--profiles`` flags are still accepted so that
registrations written against earlier versions keep starting, but they no
longer decide anything. Refusing to start on them would break every existing
config on upgrade; acting on them would leave two places to look for the same
answer, which is the problem being fixed.
"""

from __future__ import annotations

import argparse
import sys

from mysql_runner.mcp.policy import McpPolicy
from mysql_runner.mcp.server import MCPServer
from mysql_runner.mcp.tools import AppAccess

#: Flags earlier versions took. Kept parseable, deliberately inert.
LEGACY_FLAGS = (
    "--allow-write",
    "--allow-delete",
    "--allow-sql-write",
    "--allow-production",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mysql_runner.mcp",
        description=(
            "MCP server exposing Sitekeeper's stored FTP/FTPS/SFTP and MySQL "
            "connections to Claude. What it may do is set in the app, under "
            "Tools -> Connect Claude."
        ),
    )
    for flag in LEGACY_FLAGS:
        parser.add_argument(
            flag,
            action="store_true",
            help="accepted for compatibility and ignored; set this in the app",
        )
    parser.add_argument(
        "--profiles",
        default="",
        metavar="LABELS",
        help="accepted for compatibility and ignored; set this in the app",
    )
    args = parser.parse_args(argv)

    # stdout is the protocol, so every word to a human goes to stderr. This
    # one earns its place: somebody whose config still carries the flags is
    # exactly the person about to wonder why they stopped mattering.
    passed = [flag for flag in LEGACY_FLAGS if getattr(args, flag[2:].replace("-", "_"))]
    if args.profiles:
        passed.append("--profiles")
    if passed:
        print(
            "sitekeeper mcp: ignoring "
            + ", ".join(passed)
            + " - permissions now come from the app (Tools -> Connect Claude), "
            "so they are the same in every project and change without a restart.",
            file=sys.stderr,
        )
    print(f"sitekeeper mcp: {McpPolicy.load().describe()}", file=sys.stderr)
    MCPServer(AppAccess()).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
