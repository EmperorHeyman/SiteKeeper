"""MCP server: lets Claude drive Sitekeeper's servers from the outside.

``python -m mysql_runner.mcp`` speaks the Model Context Protocol over
stdio, which is how Claude Code and Claude Desktop attach local tool
servers. The tools it offers are the app's own capabilities - browse and
read remote files, push files and folders over FTP/FTPS/SFTP, run MySQL
queries - against the same encrypted profile vault the app uses.

Register it once, with nothing on the line but the program:

    claude mcp add sitekeeper -- python -m mysql_runner.mcp

Everything is read-only until the app says otherwise. What Claude may do is
ticked in Sitekeeper under Tools -> Connect Claude and read from the app's
grants file on every call, so it is the same in every project and changes
without restarting anything; connections marked production are granted one
at a time. Nothing in this package imports Qt; it runs headless.
"""

__all__: list[str] = []
