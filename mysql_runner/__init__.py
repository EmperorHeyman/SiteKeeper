"""Sitekeeper - a WinSCP-style session manager for phpMyAdmin, MySQL and SFTP.

This package is deliberately front-end agnostic. It used to re-export the Qt
entrypoint here, which meant that importing anything at all from
``mysql_runner`` - the vault, a model, an FTP client - dragged in PyQt6. That
broke the FastAPI sidecar, which is frozen without any GUI toolkit.

Import the entrypoint from where it lives instead:

    from mysql_runner.app import run

One consequence worth knowing: that re-export also imported Qt WebEngine early,
which Qt requires to happen before any QCoreApplication is constructed. Nothing
in this package can guarantee that ordering, so any GUI entrypoint has to do
what ``mysql_runner.app`` does - import ``PyQt6.QtWebEngineWidgets`` (or set
``AA_ShareOpenGLContexts``) before creating the QApplication - or browser tabs
will fail to import.
"""

#: The one place the Python side states its version. Everything else that
#: carries it - the exe resource, the installer, the frontend manifests -
#: is a build artefact; this is what the running code can actually read,
#: which is why the MCP server no longer hard-codes a number of its own.
__version__ = "1.10.1"

__all__ = ["__version__"]
