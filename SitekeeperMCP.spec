# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Sitekeeper MCP server.

A second, console executable next to the GUI one:

    pyinstaller SitekeeperMCP.spec

Output:
    dist/sitekeeper-mcp.exe

Two things make it a separate build rather than a flag on the main one.

MCP is spoken over stdin/stdout, and the GUI executable is built windowed, so
it has no console and ``sys.stdout`` is None - it physically cannot serve the
protocol. And the MCP package imports no Qt whatsoever, so excluding the whole
GUI stack here costs nothing and saves the two hundred megabytes that Qt
WebEngine accounts for in the other build.

No version resource is attached: this is a helper invoked by Claude, not
something anyone opens the properties of, and every extra copy of the version
number is one more place for it to drift.
"""

from pathlib import Path

ROOT = Path().resolve()

hiddenimports = [
    # Loaded lazily, or by name, so static analysis cannot see them.
    "pymysql",
    "paramiko",
    "keyring",
    "keyring.backends.Windows",
]

a = Analysis(
    ["mcp_main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The entire GUI stack: nothing under mysql_runner.mcp touches it.
        "PyQt6",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWidgets",
        "PyQt6.QtGui",
        "PyQt6.QtCore",
        "mysql_runner.ui",
        "mysql_runner.app",
        # And the usual dead weight.
        "tkinter",
        "turtle",
        "unittest",
        "test",
        "pydoc",
        "doctest",
        "xmlrpc",
        "matplotlib",
        "numpy",
        "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name="sitekeeper-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["python313.dll", "vcruntime140.dll", "vcruntime140_1.dll"],
    console=True,  # MCP is a stdio protocol: it needs one.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
