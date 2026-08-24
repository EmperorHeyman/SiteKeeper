# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Sitekeeper backend sidecar.

The Tauri shell spawns this as an external binary, so it must be a single
self-contained console-less exe named with the Rust target triple. Build it
through build-sidecar.ps1 rather than calling pyinstaller directly - that script
also copies the result into frontend/src-tauri/binaries with the right name.

    pyinstaller backend/mysqlrunner-backend.spec
"""

from pathlib import Path

# Paths inside a spec resolve against the spec's own directory, so the entry
# script below is relative to backend/. The repo root is one level up and has to
# be on pathex for the shared mysql_runner package.
SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent

hiddenimports = [
    # Loaded by name or through a plugin system, where static analysis cannot
    # see them.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "websockets",
    "websockets.legacy",
    # Vault + protocol backends.
    "pymysql",
    "paramiko",
    "keyring",
    "keyring.backends.Windows",
]


a = Analysis(
    ["app/main.py"],
    pathex=[str(ROOT), str(SPEC_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # No GUI in the sidecar: the whole point is that Svelte draws the UI.
        "PyQt6",
        "PyQt5",
        "tkinter",
        "turtle",
        "unittest",
        "test",
        "pydoc",
        "doctest",
        "xmlrpc",
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
    name="mysqlrunner-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["python313.dll", "vcruntime140.dll", "vcruntime140_1.dll"],
    # No console window: the shell captures stderr and forwards it to the UI.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
