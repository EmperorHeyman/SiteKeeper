# MySQL Runner — "Add" crash hotfix (1.0.3)

## Symptom
Clicking **Add** (and **Edit**) in the server list closed the whole app
instantly.

## Root cause
`MainWindow._on_add` / `_on_edit` create the profile editor like this:

```python
dialog = ServerDialog(self)                 # _on_add
dialog = ServerDialog(self, profile=profile)  # _on_edit
if dialog.exec():
    self._store.add(dialog.result_profile())   # or .update(...)
    self._refresh_server_list()
```

…but the `ServerDialog` class was **never defined or imported anywhere** in the
shipped build — its source file was missing when the 1.0.3 one-file exe was
frozen. So `ServerDialog(self)` raised `NameError: name 'ServerDialog' is not
defined`. PyQt6 aborts the process on an unhandled exception raised inside a
signal/slot handler, which is why the app simply disappeared instead of showing
an error.

(The source tree itself was gone from the repo, so the diagnosis was made by
disassembling the Python 3.13 bytecode embedded in the frozen build.)

## Fix
`server_dialog.py` restores the missing dialog. It matches the exact contract
the existing (unmodified) `main_window` bytecode expects:

* `ServerDialog(parent)` — add mode
* `ServerDialog(parent, profile=<ServerProfile>)` — edit mode (pre-fills fields)
* `.exec()` returns truthy on OK
* `.result_profile()` returns a `ServerProfile`; in edit mode it **preserves the
  original `id`** so `ServerStore.update()` can find and replace the entry.

The form exposes every `ServerProfile` field: label, url, username, password
(masked), group, auth type (Automatic / Cookie / HTTP Basic), environment
(None / Development / Staging / Production) and an optional startup-SQL box.

Because the original source is unavailable, the exe was patched **losslessly**
rather than rebuilt from scratch — see `rebuild_fixed_exe.py`. Only the embedded
PYZ changes (new `server_dialog` module + a one-line `mysql_runner.ui` shim that
publishes `ServerDialog`); all other modules and all Qt binaries are byte-for-byte
identical to the original build.

## Files
| file | purpose | canonical location if source is restored |
|------|---------|-------------------------------------------|
| `server_dialog.py` | the restored dialog | `mysql_runner/ui/server_dialog.py` |
| `ui__init__.py` | runtime shim exposing `ServerDialog` | `mysql_runner/ui/__init__.py` |
| `rebuild_fixed_exe.py` | rebuilds the fixed exe from the original build artifacts | — |

## Verification
* Unit test against the real (recovered) `ServerProfile` model: add creates a
  profile with a fresh id, edit preserves the id, values round-trip through
  serialization.
* GUI test (pywinauto) driving the frozen exe: set master password → click
  **Add** → the "Add Server" dialog opens (no crash) → save → the server is
  written to the encrypted `servers.enc` and appears in the sidebar tree.

## If you recover the full source later
Prefer the clean fix over the shim: put `server_dialog.py` in
`mysql_runner/ui/`, add `from mysql_runner.ui.server_dialog import ServerDialog`
near the other UI imports in `main_window.py`, revert `mysql_runner/ui/__init__.py`
to just its docstring, and rebuild with `pyinstaller MySQLRunner.spec`.
