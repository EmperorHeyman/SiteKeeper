"""Rebuild the fixed MySQLRunner.exe from the original (broken) 1.0.3 build.

Background
----------
The shipped 1.0.3 one-file build crashed the moment you clicked *Add* (or
*Edit*): ``MainWindow._on_add`` / ``_on_edit`` look up a module global named
``ServerDialog``, but that class was never defined or imported anywhere in the
frozen package (its source file was missing from the build). Under PyQt6 an
unhandled ``NameError`` inside a slot aborts the process, so the app just
vanished.

This script performs a *lossless* binary patch of the existing frozen build:

  * It rebuilds only the embedded PYZ archive, adding a new module
    ``mysql_runner.ui.server_dialog`` (see server_dialog.py) and replacing the
    empty ``mysql_runner.ui`` __init__ with a tiny shim (see ui__init__.py)
    that exposes ``ServerDialog`` so the *unmodified* main_window bytecode can
    resolve it at runtime.
  * Every other Python module and all ~196 MB of Qt/WebEngine binaries are
    copied through byte-for-byte, so nothing else about the app changes.

It requires the original build artifacts (present in this repo):
    build_onefile_upx/MySQLRunner/PYZ-00.pyz
    build_onefile_upx/MySQLRunner/MySQLRunner.pkg
    dist_onefile_upx/MySQLRunner.exe        (the original, broken exe)

Run with the same Python (3.13) used to produce the build:
    python hotfix/rebuild_fixed_exe.py
Output:
    dist_onefile_upx/MySQLRunner.exe        (patched in place; original backed
                                             up to MySQLRunner.exe.broken-1.0.3)

NOTE: If you ever recover the full source tree, the cleaner fix is to drop
server_dialog.py into mysql_runner/ui/ and add
``from mysql_runner.ui.server_dialog import ServerDialog`` to main_window.py,
then rebuild normally with PyInstaller. The __init__ shim exists only for this
source-less binary patch.
"""
import os
import struct
import importlib.util

from PyInstaller.archive.readers import ZlibArchiveReader
from PyInstaller.archive.writers import ZlibArchiveWriter
from PyInstaller.loader.pyimod01_archive import (
    PYZ_ITEM_MODULE, PYZ_ITEM_PKG, PYZ_ITEM_NSPKG,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTFIX = os.path.join(ROOT, "hotfix")
BUILD = os.path.join(ROOT, "build_onefile_upx", "MySQLRunner")
ORIG_PYZ = os.path.join(BUILD, "PYZ-00.pyz")
ORIG_PKG = os.path.join(BUILD, "MySQLRunner.pkg")
EXE = os.path.join(ROOT, "dist_onefile_upx", "MySQLRunner.exe")
NEW_PYZ = os.path.join(HOTFIX, "_PYZ-patched.pyz")

# CArchive on-disk structures (see PyInstaller.archive.readers.CArchiveReader)
COOKIE_FMT = "!8sIIII64s"
COOKIE_LEN = struct.calcsize(COOKIE_FMT)
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
TOC_HDR_FMT = "!IIIIBc"
TOC_HDR_LEN = struct.calcsize(TOC_HDR_FMT)


def compile_src(path, filename):
    with open(path, "r", encoding="utf-8") as fh:
        return compile(fh.read(), filename, "exec")


def build_new_pyz():
    srv = compile_src(os.path.join(HOTFIX, "server_dialog.py"),
                      r"mysql_runner\ui\server_dialog.py")
    ui_init = compile_src(os.path.join(HOTFIX, "ui__init__.py"),
                         r"mysql_runner\ui\__init__.py")

    reader = ZlibArchiveReader(ORIG_PYZ)
    entries, code_dict = [], {}
    for name, (typecode, _off, _len) in reader.toc.items():
        if typecode == PYZ_ITEM_PKG:
            src_path = name.replace(".", "/") + "/__init__.py"
        elif typecode == PYZ_ITEM_NSPKG:
            src_path = None
        else:
            src_path = name.replace(".", "/") + ".py"
        entries.append((name, src_path, "PYMODULE"))
        code_dict[name] = reader.extract(name)

    assert "mysql_runner.ui" in code_dict
    assert "mysql_runner.ui.server_dialog" not in code_dict
    code_dict["mysql_runner.ui"] = ui_init
    entries.append(("mysql_runner.ui.server_dialog",
                    "mysql_runner/ui/server_dialog.py", "PYMODULE"))
    code_dict["mysql_runner.ui.server_dialog"] = srv

    ZlibArchiveWriter(NEW_PYZ, entries, code_dict=code_dict)
    return open(NEW_PYZ, "rb").read()


def splice(new_pyz):
    pkg = bytearray(open(ORIG_PKG, "rb").read())
    cookie_start = pkg.rfind(COOKIE_MAGIC)
    magic, arch_len, toc_offset, toc_len, pyvers, pylib = struct.unpack(
        COOKIE_FMT, pkg[cookie_start:cookie_start + COOKIE_LEN])

    toc_bytes = bytearray(pkg[toc_offset:toc_offset + toc_len])
    pos, pyz_off, pyz_len, pyz_pos = 0, None, None, None
    while pos < len(toc_bytes):
        elen, off, length, ulen, flag, tc = struct.unpack(
            TOC_HDR_FMT, toc_bytes[pos:pos + TOC_HDR_LEN])
        if tc == b"z":
            pyz_off, pyz_len, pyz_pos = off, length, pos
        pos += elen
    assert pyz_off + pyz_len == toc_offset, "PYZ is not the last data blob"

    new_len = len(new_pyz)
    elen, off, length, ulen, flag, tc = struct.unpack(
        TOC_HDR_FMT, toc_bytes[pyz_pos:pyz_pos + TOC_HDR_LEN])
    toc_bytes[pyz_pos:pyz_pos + TOC_HDR_LEN] = struct.pack(
        TOC_HDR_FMT, elen, off, new_len, new_len, flag, tc)

    new_toc_offset = pyz_off + new_len
    new_arch_len = new_toc_offset + len(toc_bytes) + COOKIE_LEN
    new_cookie = struct.pack(COOKIE_FMT, magic, new_arch_len, new_toc_offset,
                             len(toc_bytes), pyvers, pylib)
    return bytes(pkg[:pyz_off]) + new_pyz + bytes(toc_bytes) + new_cookie


def main():
    assert importlib.util.MAGIC_NUMBER.hex() == "f30d0d0a", (
        "This build targets CPython 3.13; run with a 3.13 interpreter.")
    # Always derive the bootloader from the *original* (broken) exe so this
    # script is re-runnable even after the fixed exe has been written in place.
    backup = EXE + ".broken-1.0.3"
    src_exe = backup if os.path.exists(backup) else EXE
    exe = open(src_exe, "rb").read()
    orig_pkg = open(ORIG_PKG, "rb").read()
    assert exe.endswith(orig_pkg), (
        f"{os.path.basename(src_exe)} does not end with the original PKG - "
        "point this script at the pristine 1.0.3 build artifacts.")
    bootloader = exe[:len(exe) - len(orig_pkg)]

    new_pyz = build_new_pyz()
    new_pkg = splice(new_pyz)

    backup = EXE + ".broken-1.0.3"
    if not os.path.exists(backup):
        os.replace(EXE, backup)
        print("backed up original ->", backup)
    with open(EXE, "wb") as fh:
        fh.write(bootloader)
        fh.write(new_pkg)
    os.remove(NEW_PYZ)
    print("wrote fixed exe ->", EXE, os.path.getsize(EXE), "bytes")


if __name__ == "__main__":
    main()
