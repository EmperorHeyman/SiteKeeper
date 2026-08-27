"""MCP server: lets Claude drive Sitekeeper's servers from the outside.

``python -m mysql_runner.mcp`` speaks the Model Context Protocol over
stdio, which is how Claude Code and Claude Desktop attach local tool
servers. The tools it offers are the app's own capabilities - browse and
read remote files, push files and folders over FTP/FTPS/SFTP, run MySQL
queries - against the same encrypted profile vault the app uses.

Register it once:

    claude mcp add sitekeeper -- python -m mysql_runner.mcp --allow-write

Everything is read-only until a flag says otherwise (see ``--help``), and
profiles marked as production refuse writes without ``--allow-production``.
Nothing in this package imports Qt; it runs headless.
"""

__all__: list[str] = []
