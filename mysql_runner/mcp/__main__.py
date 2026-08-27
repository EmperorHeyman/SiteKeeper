"""Entry point: ``python -m mysql_runner.mcp`` starts the MCP server.

Register it with Claude Code from anywhere:

    claude mcp add sitekeeper -- python -m mysql_runner.mcp --allow-write

(or the equivalent block in Claude Desktop's config). Every permission is
off until its flag grants it, so the default server can look but not touch.
"""

from __future__ import annotations

import argparse
import sys

from mysql_runner.mcp.server import MCPServer
from mysql_runner.mcp.tools import AppAccess, Policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mysql_runner.mcp",
        description=(
            "MCP server exposing Sitekeeper's stored FTP/FTPS/SFTP and MySQL "
            "profiles to Claude. Read-only unless flags below say otherwise."
        ),
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="allow uploads and creating remote directories",
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help="allow deleting remote files and directories",
    )
    parser.add_argument(
        "--allow-sql-write",
        action="store_true",
        help="allow SQL statements that change data (default: SELECT-style only)",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="let the flags above act on profiles marked PRODUCTION",
    )
    parser.add_argument(
        "--profiles",
        default="",
        metavar="LABELS",
        help="comma-separated profile labels this server may use (default: all)",
    )
    args = parser.parse_args(argv)
    policy = Policy(
        allow_write=args.allow_write,
        allow_delete=args.allow_delete,
        allow_sql_write=args.allow_sql_write,
        allow_production=args.allow_production,
        profiles=tuple(
            label.strip() for label in args.profiles.split(",") if label.strip()
        ),
    )
    print(f"sitekeeper mcp: {policy.describe()}", file=sys.stderr)
    MCPServer(AppAccess(policy)).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
