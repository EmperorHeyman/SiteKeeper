"""UI subpackage."""
# --- recovery shim -------------------------------------------------------
# The shipped 1.0.3 build was missing the ServerDialog class that
# MainWindow._on_add / _on_edit look up as a module global, which crashed the
# app on "Add"/"Edit". We restore it here and expose it as a builtin so the
# existing (unmodified) main_window bytecode resolves the name at runtime.
import builtins as _builtins

from mysql_runner.ui.server_dialog import ServerDialog as _ServerDialog

_builtins.ServerDialog = _ServerDialog
