"""Application settings dialog (appearance, security, file transfer)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.crypto import vault as vault_mod
from mysql_runner.storage.settings import (
    MAX_TRANSFER_RATE_KB,
    MAX_TRANSFER_WORKERS,
    Settings,
)
from mysql_runner.transfer import editors
from mysql_runner.transfer import spawn

# (label, minutes) — 0 means "never auto-lock".
_LOCK_CHOICES = [
    ("Never", 0),
    ("After 1 minute", 1),
    ("After 5 minutes", 5),
    ("After 15 minutes", 15),
    ("After 30 minutes", 30),
    ("After 1 hour", 60),
]

_PROTECTION_CHOICES = [
    ("Master password", vault_mod.PROTECTION_PASSWORD),
    ("No password — tied to this Windows account", vault_mod.PROTECTION_WINDOWS),
]

#: Object name the application stylesheet renders as a grey note.
_HINT_ROLE = "hint"

_PROTECTION_HINT = {
    vault_mod.PROTECTION_PASSWORD: (
        "A master password unlocks the vault. Strongest option: the encryption "
        "key cannot be recovered from the files alone."
    ),
    vault_mod.PROTECTION_WINDOWS: (
        "No prompt, ever. Your saved connections stay encrypted, but the key is "
        "sealed to this Windows user account — anyone who can log in as you (or "
        "run code as you) can open them. Copies of the files are useless on "
        "another account or machine."
    ),
}


class SettingsDialog(QDialog):
    """Edits user preferences. Read the getters after :meth:`exec` returns truthy."""

    def __init__(self, settings: Settings, parent=None, *, on_change_password=None) -> None:
        super().__init__(parent)
        self._on_change_password = on_change_password
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        general = QWidget()
        layout = QVBoxLayout(general)
        tabs.addTab(general, "General")
        outer.addWidget(tabs)

        # ----- Appearance -------------------------------------------------
        appearance = QGroupBox("Appearance")
        appearance_form = QFormLayout(appearance)
        self._dark = QCheckBox("Dark app theme")
        self._dark.setToolTip("The window, tabs, tables and dialogs")
        self._dark.setChecked(settings.dark_mode)
        self._web_dark = QCheckBox("Dark phpMyAdmin pages")
        self._web_dark.setToolTip(
            "Darkens the phpMyAdmin page itself with the bundled Dark Reader. "
            "Independent of the app theme."
        )
        self._web_dark.setChecked(settings.web_dark_mode)
        self._sidebar = QCheckBox("Show server sidebar")
        self._sidebar.setChecked(settings.sidebar_visible)
        self._split = QCheckBox("Split view (two tab panes side by side)")
        self._split.setChecked(settings.split_view)
        appearance_form.addRow(self._dark)
        appearance_form.addRow(self._web_dark)
        appearance_form.addRow(self._sidebar)
        appearance_form.addRow(self._split)
        layout.addWidget(appearance)

        # ----- Password & locking ----------------------------------------
        security = QGroupBox("Password && locking")
        sec_form = QFormLayout(security)

        # Whether there is a master password at all.
        self._protection = QComboBox()
        for text, mode in _PROTECTION_CHOICES:
            self._protection.addItem(text, mode)
        self._initial_protection = vault_mod.protection_mode()
        index = self._protection.findData(self._initial_protection)
        if index >= 0:
            self._protection.setCurrentIndex(index)
        self._protection.currentIndexChanged.connect(self._sync_protection)
        sec_form.addRow("Vault protection:", self._protection)

        self._protection_hint = QLabel("")
        self._protection_hint.setWordWrap(True)
        self._protection_hint.setObjectName(_HINT_ROLE)
        sec_form.addRow(self._protection_hint)

        # The headline option: unlock once and never get pestered again.
        self._stay = QCheckBox("Stay logged in (unlock once, never ask again)")
        self._stay.setChecked(settings.stay_logged_in)
        self._stay.toggled.connect(self._sync_stay_logged_in)
        sec_form.addRow(self._stay)

        stay_hint = QLabel(
            "Keeps you signed in until you click “Lock” or quit. Your "
            "connections stay encrypted — this just stops the app from "
            "re-asking for the master password."
        )
        stay_hint.setWordWrap(True)
        stay_hint.setObjectName(_HINT_ROLE)
        sec_form.addRow(stay_hint)

        self._lock = QComboBox()
        for label, minutes in _LOCK_CHOICES:
            self._lock.addItem(label, minutes)
        self._select_lock(settings.idle_lock_minutes)
        sec_form.addRow("Auto-lock when idle:", self._lock)

        self._ask_start = QCheckBox("Ask for master password when the app starts")
        self._ask_start.setChecked(settings.ask_password_on_start)
        sec_form.addRow(self._ask_start)

        self._remember = QCheckBox("Remember password (don't ask again after locking)")
        self._remember.setChecked(settings.remember_password)
        sec_form.addRow(self._remember)

        self._change_btn = QPushButton("Change master password…")
        self._change_btn.clicked.connect(self._on_change_clicked)
        sec_form.addRow(self._change_btn)

        layout.addWidget(security)
        layout.addStretch(1)

        # ----- File transfer ---------------------------------------------
        tabs.addTab(self._build_transfer_tab(settings), "File transfer")

        # Reflect the initial state on the dependent controls.
        self._sync_protection()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _with_unit(spin: QSpinBox, unit: str, *, width: int = 78) -> QWidget:
        """A number box with its unit printed beside it, not inside it.

        A spin box whose unit lives in its own suffix reads as a text field
        containing the words "30 days", which raises a question nobody should
        have to ask: is "1 week" allowed? Is "7" enough, or does it need the
        word? The box holds a number and only a number; the unit is a label
        next to it that plainly cannot be typed into. It is also given a width
        that suits a number, because a field stretched across the dialog looks
        like somewhere to write a sentence.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        spin.setFixedWidth(width)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label = QLabel(unit)
        label.setObjectName(_HINT_ROLE)
        layout.addWidget(spin)
        layout.addWidget(label)
        layout.addStretch(1)
        # A special value ("Don't keep any") replaces the number entirely, so
        # the unit beside it would read as "Don't keep any days".
        if spin.specialValueText():
            def toggle(value: int, _label=label, _spin=spin) -> None:
                _label.setVisible(value != _spin.minimum())

            spin.valueChanged.connect(toggle)
            toggle(spin.value())
        return row

    # ----- file transfer --------------------------------------------------
    def _build_transfer_tab(self, settings: Settings) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)

        speed = QGroupBox("Speed")
        speed_form = QFormLayout(speed)
        self._workers = QSpinBox()
        self._workers.setRange(1, MAX_TRANSFER_WORKERS)
        self._workers.setValue(settings.transfer_workers)
        self._workers.setToolTip(
            "Each one is a separate connection. Three is a good default; shared "
            "hosting sometimes limits how many you may open."
        )
        speed_form.addRow(
            "Files at once:", self._with_unit(self._workers, "connections")
        )
        speed_note = QLabel(
            "Browsing always uses its own connection, so a running queue never "
            "blocks the file panes. A deploy of many small files is limited by "
            "round trips rather than bandwidth, so its time divides almost "
            "exactly by this number - raise it if your server allows it, and "
            "Sitekeeper will quietly settle for fewer if it does not."
        )
        speed_note.setWordWrap(True)
        speed_note.setObjectName(_HINT_ROLE)
        speed_form.addRow(speed_note)

        self._rate_limit = QSpinBox()
        self._rate_limit.setRange(0, MAX_TRANSFER_RATE_KB)
        self._rate_limit.setSingleStep(64)
        self._rate_limit.setValue(settings.transfer_rate_kb)
        self._rate_limit.setSpecialValueText("No limit")
        self._rate_limit.setToolTip(
            "The ceiling applies to everything this tab is transferring "
            "together, not to each connection, so raising the number above "
            "does not raise this one with it. Takes effect immediately - "
            "including part-way through a file already going up."
        )
        speed_form.addRow(
            "Limit speed to:", self._with_unit(self._rate_limit, "KB/s", width=96)
        )
        rate_note = QLabel(
            "Leave this at No limit unless somebody else is sharing the line: "
            "a queue at full tilt takes the whole uplink with it."
        )
        rate_note.setWordWrap(True)
        rate_note.setObjectName(_HINT_ROLE)
        speed_form.addRow(rate_note)
        self._folder_stats = QCheckBox(
            "Show folders' real size and newest change date"
        )
        self._folder_stats.setChecked(settings.folder_stats)
        self._folder_stats.setToolTip(
            "Servers report a folder's own timestamp, which does not change "
            "when a file inside it does. This walks the folder instead."
        )
        speed_form.addRow(self._folder_stats)
        self._sync_hashes = QCheckBox(
            "Compare synced folders by content, not by timestamp"
        )
        self._sync_hashes.setChecked(settings.sync_compare_hashes)
        self._sync_hashes.setToolTip(
            "A file's timestamp says when git wrote it, not when its contents "
            "were written: after a clone, a pull or a checkout, identical "
            "files look newer than the copies already on the server and every "
            "sync re-uploads the whole tree. Comparing content is slower - on "
            "a server with a shell, one command for the whole remote side - "
            "and it is right on every machine. Turn this off only where "
            "hashing is too slow and the timestamps can be trusted."
        )
        speed_form.addRow(self._sync_hashes)
        page_layout.addWidget(speed)

        safety = QGroupBox("Safety")
        safety_form = QFormLayout(safety)
        self._atomic = QCheckBox("Upload to a temporary name, then rename (safe deploy)")
        self._atomic.setChecked(settings.atomic_uploads)
        self._atomic.setToolTip(
            "A live request can never see a half-written file."
        )
        self._shadow = QCheckBox("Keep the previous version of anything overwritten")
        self._shadow.setChecked(settings.shadow_backups)
        self._verify = QCheckBox("Check each upload by comparing digests afterwards")
        self._verify.setChecked(settings.verify_uploads)
        self._preserve_times = QCheckBox(
            "Give uploaded files the same modified date as your local copy"
        )
        self._preserve_times.setChecked(settings.preserve_times)
        self._preserve_times.setToolTip(
            "Costs one round trip per file. On a deploy of thousands of small "
            "files that is roughly a seventh of the whole thing, because such "
            "a deploy is limited by round trips rather than by bandwidth. "
            "Syncs compare content now, so turning this off changes nothing "
            "except the dates shown on the server."
        )
        self._guard = QCheckBox("Ask before anything destructive on production")
        self._guard.setChecked(settings.production_guard)
        silenced = len(settings.production_guard_off)
        self._guard.setToolTip(
            "Each warning can also be switched off for its own connection. "
            + (
                f"{silenced} connection(s) have done that; turning this off "
                "and on again asks everywhere once more."
                if silenced
                else "None have done that so far."
            )
        )
        self._history_days = QSpinBox()
        self._history_days.setRange(0, 365)
        self._history_days.setValue(settings.history_days)
        self._history_days.setSpecialValueText("Don't keep any")
        safety_form.addRow(self._atomic)
        safety_form.addRow(self._shadow)
        # The unit label belongs to the field, so it greys out with it.
        self._history_row = self._with_unit(self._history_days, "days")
        safety_form.addRow("Keep those copies for:", self._history_row)
        safety_form.addRow(self._verify)
        safety_form.addRow(self._preserve_times)
        safety_form.addRow(self._guard)
        page_layout.addWidget(safety)

        behaviour = QGroupBox("Behaviour")
        behaviour_form = QFormLayout(behaviour)
        self._ignore_rules = QCheckBox("Obey .deployignore / .gitignore")
        self._ignore_rules.setChecked(settings.use_ignore_rules)
        self._ignore_defaults = QCheckBox(
            "Also skip node_modules, vendor, .git, caches and .env"
        )
        self._ignore_defaults.setChecked(settings.ignore_defaults)
        self._mirror = QCheckBox("Mirror navigation between the two panes")
        self._mirror.setChecked(settings.mirror_navigation)
        self._autosync = QCheckBox("Upload files as soon as you save them")
        self._autosync.setChecked(settings.watch_autosync)
        self._autosync.setToolTip(
            "Applies to the Watch switch above the file panes, not to synced "
            "folders, which have their own trigger."
        )
        autosync_note = QLabel(
            "Turning on Watch in a tab makes Sitekeeper notice files as your "
            "editor writes them. On its own it only tells you what changed; "
            "with this ticked it also sends each file to the folder open on "
            "the right, as soon as the file stops changing. Off is the safer "
            "default - nothing leaves this machine until you press Upload."
        )
        autosync_note.setWordWrap(True)
        autosync_note.setObjectName(_HINT_ROLE)

        self._mirror.setToolTip(
            "Entering a folder on one side opens the folder of the same name "
            "on the other, when there is one."
        )
        self._ignore_rules.setToolTip(
            "The rules in a .deployignore beside your files - or a .gitignore "
            "when there is no .deployignore - decide what batch uploads, "
            "synced folders and comparisons leave alone."
        )
        self._ignore_defaults.setToolTip(
            "A built-in list on top of your own rules, for the folders nobody "
            "means to deploy."
        )
        behaviour_form.addRow(self._ignore_rules)
        behaviour_form.addRow(self._ignore_defaults)
        behaviour_form.addRow(self._mirror)
        behaviour_form.addRow(self._autosync)
        behaviour_form.addRow(autosync_note)
        page_layout.addWidget(behaviour)

        terminal = QGroupBox("External programs")
        terminal_form = QFormLayout(terminal)
        self._terminal = QComboBox()
        self._terminal.addItem("The first one found", "")
        for found in spawn.detect_terminals():
            self._terminal.addItem(found.name, found.name)
        index = self._terminal.findData(settings.terminal_program)
        if index >= 0:
            self._terminal.setCurrentIndex(index)
        elif settings.terminal_program:
            self._terminal.addItem(
                f"{settings.terminal_program} (not found)", settings.terminal_program
            )
            self._terminal.setCurrentIndex(self._terminal.count() - 1)
        self._terminal_password = QCheckBox("Pass the password on the command line")
        self._terminal_password.setChecked(settings.terminal_send_password)
        self._terminal_password.setToolTip(
            "Convenient, but the password is briefly visible to anything that "
            "can list processes on this machine. A key file is used instead "
            "whenever the connection has one."
        )
        terminal_form.addRow("Terminal:", self._terminal)
        terminal_form.addRow(self._terminal_password)

        self._editor = QComboBox()
        self._editor.addItem("The first one found", "")
        for editor in editors.detect_editors():
            self._editor.addItem(editor.name, editor.name)
        index = self._editor.findData(settings.editor_program)
        if index >= 0:
            self._editor.setCurrentIndex(index)
        elif settings.editor_program:
            self._editor.addItem(
                f"{settings.editor_program} (not found)", settings.editor_program
            )
            self._editor.setCurrentIndex(self._editor.count() - 1)
        self._editor.setToolTip(
            "Which editor the “Open in VS Code” entries use. VS Code, Insiders, "
            "Cursor, VSCodium and Windsurf are looked for; they all share the "
            "same command line."
        )
        editor_note = QLabel(
            "Right-click a remote file ▸ Open in VS Code downloads it and "
            "uploads every save back. Right-click a remote folder on SFTP and "
            "VS Code opens it on the server itself, over its own SSH session - "
            "so it asks for that server's password or key itself, and needs "
            "the Remote-SSH extension installed."
        )
        editor_note.setWordWrap(True)
        editor_note.setObjectName(_HINT_ROLE)
        terminal_form.addRow("Editor:", self._editor)
        terminal_form.addRow(editor_note)
        page_layout.addWidget(terminal)

        page_layout.addStretch(1)
        self._ignore_rules.toggled.connect(self._ignore_defaults.setEnabled)
        self._ignore_defaults.setEnabled(self._ignore_rules.isChecked())
        self._shadow.toggled.connect(self._history_row.setEnabled)
        self._history_row.setEnabled(self._shadow.isChecked())
        return page

    # ----- helpers --------------------------------------------------------
    def _select_lock(self, minutes: int) -> None:
        index = self._lock.findData(minutes)
        if index < 0:
            label = "Never" if minutes <= 0 else f"After {minutes} minutes"
            self._lock.addItem(label, minutes)
            index = self._lock.count() - 1
        self._lock.setCurrentIndex(index)

    def _sync_protection(self) -> None:
        """Grey out password options when there is no master password."""
        mode = self.protection_mode()
        self._protection_hint.setText(_PROTECTION_HINT.get(mode, ""))
        has_password = mode == vault_mod.PROTECTION_PASSWORD
        self._stay.setEnabled(has_password)
        self._change_btn.setEnabled(
            has_password and mode == self._initial_protection
        )
        self._sync_stay_logged_in(self._stay.isChecked())

    def _sync_stay_logged_in(self, staying: bool) -> None:
        """Disable the granular options while they cannot take effect."""
        # Without a master password there is nothing to prompt for, and while
        # "stay logged in" is on it overrides all three anyway.
        has_password = self.protection_mode() == vault_mod.PROTECTION_PASSWORD
        active = has_password and not staying
        self._lock.setEnabled(active)
        self._ask_start.setEnabled(active)
        self._remember.setEnabled(active)

    def _on_change_clicked(self) -> None:
        if self._on_change_password is not None:
            self._on_change_password(self)

    # ----- result accessors ----------------------------------------------
    def dark_mode(self) -> bool:
        return self._dark.isChecked()

    def web_dark_mode(self) -> bool:
        return self._web_dark.isChecked()

    def sidebar_visible(self) -> bool:
        return self._sidebar.isChecked()

    def split_view(self) -> bool:
        return self._split.isChecked()

    def idle_lock_minutes(self) -> int:
        return int(self._lock.currentData())

    def ask_password_on_start(self) -> bool:
        return self._ask_start.isChecked()

    def remember_password(self) -> bool:
        return self._remember.isChecked()

    def stay_logged_in(self) -> bool:
        return self._stay.isChecked()

    def protection_mode(self) -> str:
        return str(self._protection.currentData())

    def protection_changed(self) -> bool:
        """Whether the user picked a different protection mode."""
        return self.protection_mode() != self._initial_protection

    # ----- file transfer results -----------------------------------------
    def transfer_workers(self) -> int:
        return int(self._workers.value())

    def transfer_rate_kb(self) -> int:
        return int(self._rate_limit.value())

    def atomic_uploads(self) -> bool:
        return self._atomic.isChecked()

    def shadow_backups(self) -> bool:
        return self._shadow.isChecked()

    def history_days(self) -> int:
        return int(self._history_days.value())

    def verify_uploads(self) -> bool:
        return self._verify.isChecked()

    def preserve_times(self) -> bool:
        return self._preserve_times.isChecked()

    def use_ignore_rules(self) -> bool:
        return self._ignore_rules.isChecked()

    def ignore_defaults(self) -> bool:
        return self._ignore_defaults.isChecked()

    def folder_stats(self) -> bool:
        return self._folder_stats.isChecked()

    def sync_compare_hashes(self) -> bool:
        return self._sync_hashes.isChecked()

    def mirror_navigation(self) -> bool:
        return self._mirror.isChecked()

    def production_guard(self) -> bool:
        return self._guard.isChecked()

    def watch_autosync(self) -> bool:
        return self._autosync.isChecked()

    def terminal_program(self) -> str:
        return str(self._terminal.currentData() or "")

    def terminal_send_password(self) -> bool:
        return self._terminal_password.isChecked()

    def editor_program(self) -> str:
        return str(self._editor.currentData() or "")
