"""Console entry point for the packaged MCP server.

The GUI executable cannot serve MCP. It is built windowed (``console=False``
in Sitekeeper.spec), which on Windows means the process has no console and
``sys.stdout`` is None - and MCP is a protocol spoken over stdin/stdout. So a
build of Sitekeeper could never register itself with Claude, and the only
working command was ``python -m mysql_runner.mcp`` out of a source checkout,
which an installed user does not have.

This is that command as an executable the installer can ship. It imports no
Qt at all - the MCP package only needs the vault, the storage models and the
transfer clients - so it costs tens of megabytes rather than repeating the two
hundred the GUI build spends on Qt WebEngine.
"""

from __future__ import annotations

import sys

from mysql_runner.mcp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
