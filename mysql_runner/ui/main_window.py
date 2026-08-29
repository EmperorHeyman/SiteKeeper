"""Main application window: server sidebar + tabbed session views.

The sidebar can be expanded, collapsed to a slim rail (so there is always a way
back), or hidden outright. The tab area holds one or two panes side by side, and
each tab is chosen by the profile's connection kind: a phpMyAdmin browser view,
a native MySQL console, or a dual-pane file transfer view.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mysql_runner.crypto import vault as vault_mod
from mysql_runner.db import mysql_client
from mysql_runner.runtime_mode import running_elevated
from mysql_runner.storage.models import ConnectionKind, Environment, ServerProfile
from mysql_runner.storage.portable import PortableError, export_profiles, import_profiles
from mysql_runner.storage.settings import MIN_SIDEBAR_WIDTH, Settings
from mysql_runner.storage.store import ServerStore
from mysql_runner.transfer import connstr
from mysql_runner.ui.file_manager_tab import FileManagerTab
from mysql_runner.ui.master_password_dialog import (
    ChangeMasterPasswordDialog,
    CreateMasterPasswordDialog,
    UnlockDialog,
)
from mysql_runner.ui.server_dialog import ServerDialog
from mysql_runner.ui.settings_dialog import SettingsDialog
from mysql_runner.ui import theme
from mysql_runner.ui.sql_console_tab import SqlConsoleTab
from mysql_runner.ui.ssh_terminal_tab import SshTerminalTab
from mysql_runner.transfer import sftp_client
from mysql_runner.web.browser_tab import BrowserTab

_NO_SELECTION = "No selection"

#: The category a connection falls into when it has no group of its own.
#: The sidebar used to file everything under one "Ungrouped" heading, which
#: told you nothing: a phpMyAdmin login and an SFTP account are different
#: tools and want separating before anything else. This order is the order
#: they are drawn in.
_DEFAULT_CATEGORIES = (
    ("phpMyAdmin", (ConnectionKind.PHPMYADMIN,)),
    ("MySQL", (ConnectionKind.MYSQL,)),
    (
        "Other (FTP/SFTP)",
        (ConnectionKind.FTP, ConnectionKind.FTPS, ConnectionKind.SFTP),
    ),
)

#: kind -> default category name.
_CATEGORY_OF = {
    kind: name for name, kinds in _DEFAULT_CATEGORIES for kind in kinds
}

#: Where the default categories sit relative to the groups people name
#: themselves, which follow in alphabetical order.
_CATEGORY_ORDER = {
    name: index for index, (name, _) in enumerate(_DEFAULT_CATEGORIES)
}
#: Where a heading keeps the category name it stands for, so a drop can
#: tell which group the row landed in.
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 1

#: Width of the collapsed sidebar rail, in pixels.
_RAIL_WIDTH = 30

_ENV_COLORS = {
    Environment.PROD: QColor("#e53935"),
    Environment.STAGING: QColor("#fb8c00"),
}

_KIND_BADGES = {
    ConnectionKind.MYSQL: "sql",
    ConnectionKind.FTP: "ftp",
    ConnectionKind.FTPS: "ftps",
    ConnectionKind.SFTP: "sftp",
}


class MainWindow(QMainWindow):
    """Sidebar list of saved connections plus a tab per open session."""

    def __init__(
        self,
        store: ServerStore,
        settings: Settings | None = None,
        on_lock=None,
        on_settings_changed=None,
    ) -> None:
        super().__init__()
        self._store = store
        self._settings = settings or Settings()
        self._on_lock = on_lock
        self._on_settings_changed = on_settings_changed
        # Reference counts for engine profiles shared between cloned tabs.
        self._profile_refs: dict[object, int] = {}
        self._panes: list[QTabWidget] = []
        self._active_pane = 0
        self.setWindowTitle("Sitekeeper")
        self._size_to_screen()

        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._refresh_server_list()
        self._apply_settings()

    def _size_to_screen(self) -> None:
        """Open at a size that suits the monitor, not a fixed 1200x800.

        Two file panes, a sidebar and a queue want room, and a fixed size that
        was generous on a laptop is a postage stamp on the 1440p and 4K screens
        this is actually used on. Takes most of the available work area - which
        already excludes the taskbar - keeps a sane floor for small displays,
        and centres what it opens.
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1440, 900)
            return
        available = screen.availableGeometry()
        width = max(1200, int(available.width() * 0.82))
        height = max(800, int(available.height() * 0.86))
        width = min(width, available.width())
        height = min(height, available.height())
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    # ----- UI construction ----------------------------------------------
    def _build_ui(self) -> None:
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self._sidebar_host = QWidget()
        self._sidebar_host.setObjectName("sidebarhost")
        host_layout = QHBoxLayout(self._sidebar_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)
        host_layout.addWidget(self._build_sidebar())
        host_layout.addWidget(self._build_rail())

        self._pane_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._panes.append(self._create_pane())
        self._pane_splitter.addWidget(self._panes[0])

        self._splitter.addWidget(self._sidebar_host)
        self._splitter.addWidget(self._pane_splitter)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([self._settings.sidebar_width, 920])
        # Dragging the handle all the way in should collapse, not vanish.
        self._splitter.setChildrenCollapsible(False)
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        # A line between the menu bar and the working area: without it the two
        # ran together and the window looked like one undivided field.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(theme.divider())
        central_layout.addWidget(self._splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(_startup_message())

    def _build_sidebar(self) -> QWidget:
        self._sidebar = QWidget()
        self._sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(self._sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Connections")
        title.setObjectName("title")
        collapse = QToolButton()
        collapse.setIcon(theme.nav_icon("back", self._settings.dark_mode))
        collapse.setIconSize(QSize(14, 14))
        collapse.setToolTip("Collapse the sidebar (Ctrl+B)")
        collapse.setAutoRaise(True)
        collapse.setFixedSize(24, 22)
        collapse.clicked.connect(lambda: self._set_sidebar_collapsed(True))
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(collapse)
        sidebar_layout.addLayout(header)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search servers…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        sidebar_layout.addWidget(self._search)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        # Qt's default indent is sized for trees that nest; this one is two
        # levels deep and every connection carries an icon that says what it
        # is, so the default spent a third of a narrow sidebar on empty space
        # to the left of every row.
        # No indent and no expander column. Qt paints a row's selection across
        # the branch column as a separate rectangle, which showed up as a small
        # grey tab floating to the left of the current connection, detached
        # from its own rounded highlight - and no amount of styling that column
        # would stop it, because the highlight is supposed to be one shape.
        # Removing the column removes the question: this tree is two levels
        # deep, its headings are already unmistakably headings, and every
        # connection carries an icon.
        self._tree.setIndentation(0)
        self._tree.setRootIsDecorated(False)
        # Connections can be dragged into the order you want, and onto another
        # heading to move them into that group. Nothing else in this list is
        # draggable: a heading is a place, not a thing.
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._tree.setDragEnabled(True)
        self._tree.viewport().setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.model().rowsMoved.connect(self._on_rows_moved)
        self._tree.itemDoubleClicked.connect(self._on_item_activated)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_menu)
        sidebar_layout.addWidget(self._tree)

        # Six identical buttons in three rows said that opening a
        # connection - the entire purpose of this list - was exactly as
        # important as locking the vault. Connect leads, and the three that
        # need something selected say so by going quiet until it is.
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setObjectName("primary")
        self._connect_btn.setToolTip("Open the selected connection (Enter)")
        self._connect_btn.clicked.connect(self._on_connect)
        sidebar_layout.addWidget(self._connect_btn)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        add_btn = QPushButton("Add")
        self._edit_btn = QPushButton("Edit")
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setObjectName("danger")
        add_btn.clicked.connect(self._on_add)
        self._edit_btn.clicked.connect(self._on_edit)
        self._delete_btn.clicked.connect(self._on_delete)
        button_row.addWidget(add_btn)
        button_row.addWidget(self._edit_btn)
        button_row.addWidget(self._delete_btn)
        sidebar_layout.addLayout(button_row)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._open_settings)
        lock_btn = QPushButton("Lock")
        lock_btn.clicked.connect(self._on_lock_clicked)
        bottom_row.addWidget(settings_btn)
        bottom_row.addWidget(lock_btn)
        sidebar_layout.addLayout(bottom_row)

        self._tree.itemSelectionChanged.connect(self._refresh_sidebar_actions)
        self._refresh_sidebar_actions()
        return self._sidebar

    def _refresh_sidebar_actions(self) -> None:
        """Only offer what the current selection can actually do."""
        chosen = self._selected_profile() is not None
        for button, empty in (
            (self._connect_btn, "Pick a connection in the list first"),
            (self._edit_btn, "Pick a connection to edit"),
            (self._delete_btn, "Pick a connection to delete"),
        ):
            button.setEnabled(chosen)
            if not chosen:
                button.setToolTip(empty)
        if chosen:
            self._connect_btn.setToolTip("Open the selected connection (Enter)")
            self._edit_btn.setToolTip("Change this connection's settings")
            self._delete_btn.setToolTip("Forget this connection")

    def _build_rail(self) -> QWidget:
        """The slim strip shown while the sidebar is collapsed."""
        self._rail = QWidget()
        self._rail.setObjectName("rail")
        self._rail.setFixedWidth(_RAIL_WIDTH)
        rail_layout = QVBoxLayout(self._rail)
        rail_layout.setContentsMargins(2, 6, 2, 6)
        rail_layout.setSpacing(6)

        expand = QToolButton()
        expand.setIcon(theme.nav_icon("forward", self._settings.dark_mode))
        expand.setIconSize(QSize(14, 14))
        expand.setToolTip("Show the sidebar (Ctrl+B)")
        expand.setAutoRaise(True)
        expand.setFixedSize(_RAIL_WIDTH - 6, 22)
        expand.clicked.connect(lambda: self._set_sidebar_collapsed(False))
        rail_layout.addWidget(expand)

        # Cheap vertical caption: one character per line.
        caption = QLabel("\n".join("CONNECTIONS"))
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        caption.setObjectName("hint")
        caption.setStyleSheet("font-size: 9px;")
        rail_layout.addWidget(caption)
        rail_layout.addStretch(1)
        self._rail.setVisible(False)
        return self._rail

    def _create_pane(self) -> QTabWidget:
        """Build one tab pane and wire it to this window."""
        pane = QTabWidget()
        pane.setTabsClosable(False)  # a quieter close button is installed per tab
        pane.setMovable(True)
        pane.setDocumentMode(True)
        pane.tabCloseRequested.connect(
            lambda index, p=pane: self._on_tab_close(p, index)
        )
        pane.tabBarClicked.connect(lambda _index, p=pane: self._set_active_pane(p))
        pane.currentChanged.connect(lambda _index, p=pane: self._set_active_pane(p))
        return pane

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        export_action = QAction("&Export connections…", self)
        export_action.triggered.connect(self._on_export)
        import_action = QAction("&Import connections…", self)
        import_action.triggered.connect(self._on_import)
        winscp_action = QAction("Import from &WinSCP or a URL list…", self)
        winscp_action.setToolTip(
            "Read WinSCP.ini, or a file of sftp:// connection strings"
        )
        winscp_action.triggered.connect(self._on_import_winscp)
        paste_action = QAction("Add from a connection &string…", self)
        paste_action.triggered.connect(self._on_paste_connection)
        winscp_export_action = QAction("Export &for WinSCP…", self)
        winscp_export_action.triggered.connect(self._on_export_winscp)
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        lock_action = QAction("&Lock now", self)
        lock_action.triggered.connect(self._on_lock_clicked)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(export_action)
        file_menu.addAction(import_action)
        file_menu.addSeparator()
        file_menu.addAction(winscp_action)
        file_menu.addAction(paste_action)
        file_menu.addAction(winscp_export_action)
        file_menu.addSeparator()
        file_menu.addAction(settings_action)
        file_menu.addAction(lock_action)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("&View")
        self._collapse_action = QAction("&Collapse sidebar", self)
        self._collapse_action.setCheckable(True)
        self._collapse_action.setChecked(self._settings.sidebar_collapsed)
        self._collapse_action.setShortcut("Ctrl+B")
        self._collapse_action.triggered.connect(
            lambda checked: self._set_sidebar_collapsed(checked)
        )
        self._sidebar_action = QAction("&Hide sidebar completely", self)
        self._sidebar_action.setCheckable(True)
        self._sidebar_action.setChecked(not self._settings.sidebar_visible)
        self._sidebar_action.setShortcut("Ctrl+Shift+B")
        self._sidebar_action.triggered.connect(
            lambda checked: self._set_sidebar_hidden(checked)
        )
        self._split_action = QAction("&Split view (side by side)", self)
        self._split_action.setCheckable(True)
        self._split_action.setChecked(self._settings.split_view)
        self._split_action.setShortcut("Ctrl+Alt+S")
        self._split_action.triggered.connect(
            lambda checked: self._set_split_view(checked)
        )
        self._dark_action = QAction("&Dark app theme", self)
        self._dark_action.setCheckable(True)
        self._dark_action.setChecked(self._settings.dark_mode)
        self._dark_action.setShortcut("Ctrl+Shift+D")
        self._dark_action.setToolTip("The window, tabs, tables and dialogs")
        self._dark_action.triggered.connect(self._toggle_dark_mode)
        self._web_dark_action = QAction("Dark &phpMyAdmin pages", self)
        self._web_dark_action.setCheckable(True)
        self._web_dark_action.setChecked(self._settings.web_dark_mode)
        self._web_dark_action.setShortcut("Ctrl+Shift+W")
        self._web_dark_action.setToolTip(
            "Darkens the phpMyAdmin page itself, with the bundled Dark Reader"
        )
        self._web_dark_action.triggered.connect(self._toggle_web_dark_mode)
        view_menu.addAction(self._collapse_action)
        view_menu.addAction(self._sidebar_action)
        view_menu.addSeparator()
        view_menu.addAction(self._split_action)
        view_menu.addSeparator()
        view_menu.addAction(self._dark_action)
        view_menu.addAction(self._web_dark_action)

        tab_menu = menubar.addMenu("&Tabs")
        clone_action = QAction("&Clone current tab", self)
        clone_action.setShortcut("Ctrl+D")
        clone_action.triggered.connect(self._clone_current_tab)
        close_action = QAction("C&lose current tab", self)
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(self._close_current_tab)
        move_action = QAction("&Move tab to the other pane", self)
        move_action.setShortcut("Ctrl+Alt+M")
        move_action.triggered.connect(self._move_current_tab_across)
        focus_action = QAction("Switch to the other &pane", self)
        focus_action.setShortcut("Ctrl+Alt+Tab")
        focus_action.triggered.connect(self._focus_other_pane)
        tab_menu.addAction(clone_action)
        tab_menu.addAction(close_action)
        tab_menu.addSeparator()
        tab_menu.addAction(move_action)
        tab_menu.addAction(focus_action)

        tools_menu = menubar.addMenu("T&ools")
        mcp_action = QAction("Connect &Claude (MCP server)…", self)
        mcp_action.setToolTip(
            "Let Claude Code / Claude Desktop use these connections"
        )
        mcp_action.triggered.connect(self._show_mcp_hint)
        tools_menu.addAction(mcp_action)

    def _build_shortcuts(self) -> None:
        # Cycle tabs.
        nxt = QShortcut(QKeySequence("Ctrl+Tab"), self)
        nxt.activated.connect(lambda: self._cycle_tab(1))
        prev = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        prev.activated.connect(lambda: self._cycle_tab(-1))
        # Jump to tab 1-9 in the active pane.
        for i in range(1, 10):
            sc = QShortcut(QKeySequence(f"Ctrl+{i}"), self)
            sc.activated.connect(lambda idx=i - 1: self._goto_tab(idx))

    def _apply_settings(self) -> None:
        self._sidebar_host.setVisible(self._settings.sidebar_visible)
        self._set_sidebar_collapsed(self._settings.sidebar_collapsed, persist=False)
        self._set_split_view(self._settings.split_view, persist=False)

    # ----- panes ---------------------------------------------------------
    def _pane(self) -> QTabWidget:
        """The pane new tabs open in."""
        index = min(self._active_pane, len(self._panes) - 1)
        return self._panes[index]

    def _set_active_pane(self, pane: QTabWidget) -> None:
        if pane in self._panes:
            self._active_pane = self._panes.index(pane)

    def _set_split_view(self, enabled: bool, *, persist: bool = True) -> None:
        if enabled and len(self._panes) == 1:
            second = self._create_pane()
            self._panes.append(second)
            self._pane_splitter.addWidget(second)
            sizes = self._settings.split_sizes or [1, 1]
            self._pane_splitter.setSizes(sizes)
        elif not enabled and len(self._panes) == 2:
            self._settings.split_sizes = list(self._pane_splitter.sizes())
            closing = self._panes.pop()
            # Nothing may be lost when the pane goes away: move its tabs over.
            while closing.count():
                self._move_tab(closing, self._panes[0], 0)
            self._active_pane = 0
            closing.setParent(None)
            closing.deleteLater()
        self._split_action.setChecked(len(self._panes) == 2)
        if persist:
            self._settings.split_view = len(self._panes) == 2
            self._settings.save()

    def _move_tab(self, source: QTabWidget, target: QTabWidget, index: int) -> None:
        """Move one tab between panes, keeping its label, colour and tooltip."""
        widget = source.widget(index)
        if widget is None:
            return
        text = source.tabText(index)
        tooltip = source.tabToolTip(index)
        colour = source.tabBar().tabTextColor(index)
        source.removeTab(index)
        new_index = target.addTab(widget, text)
        # The close button belongs to the tab bar, not the widget, so a moved
        # tab needs a fresh one.
        self._install_close_button(target, new_index)
        target.setTabToolTip(new_index, tooltip)
        if colour.isValid():
            target.tabBar().setTabTextColor(new_index, colour)
        target.setCurrentIndex(new_index)

    def _move_current_tab_across(self) -> None:
        if len(self._panes) < 2:
            # Asking to move a tab is a clear signal that split view is wanted.
            self._set_split_view(True)
        source = self._pane()
        index = source.currentIndex()
        if index < 0:
            self.statusBar().showMessage("No tab to move", 4000)
            return
        target = self._panes[1] if source is self._panes[0] else self._panes[0]
        self._move_tab(source, target, index)
        self._set_active_pane(target)

    def _focus_other_pane(self) -> None:
        if len(self._panes) < 2:
            return
        self._active_pane = 0 if self._active_pane else 1
        pane = self._pane()
        pane.setFocus()
        current = pane.currentWidget()
        if current is not None:
            current.setFocus()

    def _all_tabs(self):
        """Yield (pane, index, widget) for every open tab."""
        for pane in self._panes:
            for index in range(pane.count()):
                yield pane, index, pane.widget(index)

    # ----- server list ---------------------------------------------------
    def _refresh_server_list(self) -> None:
        """Draw the sidebar: a heading per category, connections under it.

        A connection with a group of its own keeps it; everything else is
        filed by what it actually is. Empty categories are never drawn, so a
        vault of phpMyAdmin logins and SFTP accounts shows exactly two.
        """
        self._tree.clear()
        by_category: dict[str, list[ServerProfile]] = {}
        for profile in self._store.all():
            by_category.setdefault(_category_of(profile), []).append(profile)
        for name in sorted(by_category, key=_category_sort_key):
            # Hand-arranged groups keep the arrangement; the rest stay
            # alphabetical, which is what a group nobody has dragged should
            # look like.
            profiles = sorted(
                by_category[name], key=lambda p: (p.order or 0, p.label.lower())
            )
            parent = QTreeWidgetItem([f"{name}  ({len(profiles)})"])
            parent.setFirstColumnSpanned(True)
            # A heading is a drop target and nothing else: it cannot be picked
            # up, and it cannot be selected as though it were a connection.
            parent.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
            )
            parent.setData(0, _GROUP_ROLE, name)
            self._tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            for profile in profiles:
                badge = _KIND_BADGES.get(profile.kind)
                label = (
                    f"{profile.label}  ·  {badge}" if badge else profile.label
                )
                item = QTreeWidgetItem([label])
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsDragEnabled
                )
                item.setData(0, Qt.ItemDataRole.UserRole, profile.id)
                item.setIcon(
                    0, theme.kind_icon(profile.kind.value, self._settings.dark_mode)
                )
                item.setToolTip(0, profile.describe_target())
                color = _ENV_COLORS.get(profile.environment)
                if color is not None:
                    item.setForeground(0, color)
                parent.addChild(item)
        self._apply_filter(self._search.text())

    def _on_rows_moved(self, *_args) -> None:
        """A drag finished: read the tree back and write down what it says.

        Qt has already moved the row, so the tree is the truth. Every
        connection under every heading is renumbered from one, and a
        connection now sitting under a different heading is *in* that group -
        which is how a drag onto a heading moves it there. Dropping onto one
        of the default headings clears the group instead of storing its name,
        so the connection goes back to being filed by what it is.
        """
        positions: dict[str, tuple[str, int]] = {}
        for index in range(self._tree.topLevelItemCount()):
            heading = self._tree.topLevelItem(index)
            name = str(heading.data(0, _GROUP_ROLE) or "")
            # A default heading is not a group anybody named.
            group = "" if name in _CATEGORY_ORDER else name
            for position in range(heading.childCount()):
                child = heading.child(position)
                profile_id = child.data(0, Qt.ItemDataRole.UserRole)
                if profile_id:
                    positions[str(profile_id)] = (group, position + 1)
        if not positions:
            return
        changed = self._store.reorder(positions)
        # Redraw either way: Qt's move left the counts in the headings stale,
        # and a drop the store declined must not stay on screen.
        self._refresh_server_list()
        if changed:
            self.statusBar().showMessage("Connection list rearranged", 3000)

    def _apply_filter(self, text: str) -> None:
        """Hide what does not match. Hostnames count, not just labels.

        Searching for a host or a protocol is at least as common as
        searching for the name someone gave a connection, and the target is
        in the tooltip already.
        """
        needle = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            group_matches = needle in group.text(0).lower()
            visible_children = 0
            for j in range(group.childCount()):
                child = group.child(j)
                haystack = f"{child.text(0)} {child.toolTip(0)}".lower()
                match = group_matches or needle in haystack
                child.setHidden(bool(needle) and not match)
                if not child.isHidden():
                    visible_children += 1
            group.setHidden(bool(needle) and visible_children == 0)

    def _on_tree_menu(self, position) -> None:
        """Right-click the list: what the buttons underneath it can do."""
        item = self._tree.itemAt(position)
        if item is not None:
            self._tree.setCurrentItem(item)
        profile = self._selected_profile()
        menu = QMenu(self._tree)
        connect = menu.addAction("Connect", self._on_connect)
        connect.setEnabled(profile is not None)
        menu.addSeparator()
        menu.addAction("Add…", self._on_add)
        edit = menu.addAction("Edit…", self._on_edit)
        edit.setEnabled(profile is not None)
        duplicate = menu.addAction("Duplicate", self._on_duplicate)
        duplicate.setEnabled(profile is not None)
        delete = menu.addAction("Delete", self._on_delete)
        delete.setEnabled(profile is not None)
        if profile is not None:
            menu.addSeparator()
            menu.addAction("Move to a group…", self._on_change_group)
        menu.exec(self._tree.viewport().mapToGlobal(position))

    def _on_duplicate(self) -> None:
        """Copy a connection, credentials and all, under a new name."""
        profile = self._selected_profile()
        if profile is None:
            return
        data = profile.to_dict()
        data.pop("id", None)
        data["label"] = f"{profile.label} (copy)"
        self._store.add(ServerProfile.from_dict(data))
        self._refresh_server_list()
        self.statusBar().showMessage(f"Copied {profile.label}", 4000)

    def _on_change_group(self) -> None:
        """Move a connection into a group, or back to its own category."""
        profile = self._selected_profile()
        if profile is None:
            return
        known = sorted(
            {p.group.strip() for p in self._store.all() if p.group.strip()}
        )
        choices = ["", *known]
        current = profile.group.strip()
        name, ok = QInputDialog.getItem(
            self,
            "Move to a group",
            "Group (leave it empty to file this by connection type):",
            choices,
            choices.index(current) if current in choices else 0,
            True,
        )
        if not ok:
            return
        profile.group = name.strip()
        self._store.update(profile)
        self._refresh_server_list()

    def _selected_profile(self) -> ServerProfile | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        profile_id = item.data(0, Qt.ItemDataRole.UserRole)
        if profile_id is None:
            return None
        return self._store.get(profile_id)

    # ----- CRUD actions --------------------------------------------------
    def _on_add(self) -> None:
        dialog = ServerDialog(self)
        if dialog.exec():
            self._store.add(dialog.result_profile())
            self._refresh_server_list()

    def _on_edit(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, _NO_SELECTION, "Select a server to edit.")
            return
        dialog = ServerDialog(self, profile=profile)
        if dialog.exec():
            self._store.update(dialog.result_profile())
            self._refresh_server_list()

    def _on_delete(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, _NO_SELECTION, "Select a server to delete.")
            return
        confirm = QMessageBox.question(
            self,
            "Delete server",
            f"Delete '{profile.label}'? This cannot be undone.",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._store.delete(profile.id)
            self._refresh_server_list()

    # ----- export / import ----------------------------------------------
    def _on_export(self) -> None:
        profiles = self._store.all()
        if not profiles:
            QMessageBox.information(self, "Nothing to export", "No servers saved yet.")
            return
        passphrase, ok = QInputDialog.getText(
            self,
            "Export passphrase",
            "Choose a passphrase to protect the exported file:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not passphrase:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export connections", "connections.mrx", "Sitekeeper Export (*.mrx)"
        )
        if not path:
            return
        try:
            export_profiles(profiles, passphrase, path)
        except PortableError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Export complete", f"Exported {len(profiles)} server(s)."
        )

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import connections", "", "Sitekeeper Export (*.mrx);;All files (*)"
        )
        if not path:
            return
        passphrase, ok = QInputDialog.getText(
            self,
            "Import passphrase",
            "Enter the passphrase for this file:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        try:
            profiles = import_profiles(path, passphrase)
        except PortableError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        count = self._store.add_many(profiles)
        self._refresh_server_list()
        QMessageBox.information(self, "Import complete", f"Imported {count} server(s).")

    # ----- WinSCP and connection strings ---------------------------------
    def _on_import_winscp(self) -> None:
        """Adopt an existing WinSCP install, or a list of connection strings.

        WinSCP keeps its sessions in the registry unless it has been told to use
        an ini, so this looks there first and only asks for a file when nothing
        is installed - otherwise the obvious answer ("import my sessions") would
        need the user to know where WinSCP hides them.
        """
        source, found = connstr.discover_winscp()
        if found.profiles:
            confirm = QMessageBox.question(
                self,
                "Import from WinSCP",
                f"Found {len(found.profiles)} session(s) in {source}.\n\nImport them?",
            )
            if confirm == QMessageBox.StandardButton.Yes:
                self._apply_import(found)
                return
        self._import_sessions_from_file()

    def _import_sessions_from_file(self) -> None:
        """Read sessions from a WinSCP.ini or a list of connection strings."""
        start = connstr.find_winscp_ini() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import sessions",
            start,
            "WinSCP configuration (WinSCP.ini *.ini);;Connection strings (*.txt);;"
            "All files (*)",
        )
        if not path:
            return
        try:
            result = connstr.load_any(path)
        except connstr.ConnStrError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._apply_import(result)

    def _apply_import(self, result) -> None:
        """Add what an import found, and say what happened to the rest."""
        if not result.profiles:
            QMessageBox.information(
                self,
                "Nothing to import",
                "No usable sessions were found."
                + ("\n\n" + "\n".join(result.skipped[:10]) if result.skipped else ""),
            )
            return
        count = self._store.add_many(result.profiles)
        self._refresh_server_list()
        message = f"Imported {count} connection(s)."
        if result.skipped:
            message += "\n\nSkipped:\n" + "\n".join(result.skipped[:10])
        QMessageBox.information(self, "Import complete", message)

    def _on_paste_connection(self) -> None:
        """Add one server from a pasted URL."""
        text, ok = QInputDialog.getText(
            self,
            "Add from a connection string",
            "Paste a connection string:",
            QLineEdit.EchoMode.Normal,
            "sftp://user:password@host:22/var/www",
        )
        if not ok or not text.strip():
            return
        try:
            profile = connstr.parse_url(text.strip())
        except connstr.ConnStrError as exc:
            QMessageBox.critical(self, "Not a connection string", str(exc))
            return
        label, ok = QInputDialog.getText(
            self, "Name this connection", "Label:", text=profile.label
        )
        if ok and label.strip():
            profile.label = label.strip()
        self._store.add(profile)
        self._refresh_server_list()
        self.statusBar().showMessage(f"Added {profile.label}", 4000)

    def _on_export_winscp(self) -> None:
        """Write the transfer connections out in WinSCP's own format."""
        profiles = [p for p in self._store.all() if p.kind.is_transfer]
        if not profiles:
            QMessageBox.information(
                self, "Nothing to export", "No FTP or SFTP connections are saved."
            )
            return
        include = QMessageBox.question(
            self,
            "Include passwords?",
            "Include the saved passwords?\n\nWinSCP stores them scrambled, not "
            "encrypted — anyone with the file can read them back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export for WinSCP",
            "WinSCP.ini",
            "WinSCP configuration (*.ini);;Connection strings (*.txt)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".txt") or "strings" in selected.lower():
                text = connstr.to_url_list(profiles, include_passwords=include)
            else:
                text = connstr.to_winscp_ini(profiles, include_passwords=include)
            with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Export complete", f"Wrote {len(profiles)} connection(s) to {path}."
        )

    # ----- connecting ----------------------------------------------------
    def _on_item_activated(self, _item: QTreeWidgetItem) -> None:
        self._on_connect()

    def _on_connect(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            QMessageBox.information(self, _NO_SELECTION, "Select a server to connect.")
            return
        self._open_tab(profile)

    def _build_tab(self, profile: ServerProfile) -> QWidget | None:
        """Create the right tab widget for this profile's kind."""
        dark = self._settings.dark_mode
        if profile.kind == ConnectionKind.MYSQL:
            if not mysql_client.driver_available():
                QMessageBox.critical(
                    self,
                    "MySQL driver missing",
                    "This build has no MySQL driver, so SQL console tabs cannot "
                    "connect. Install PyMySQL and restart.",
                )
                return None
            return SqlConsoleTab(profile, dark_mode=dark)
        if profile.kind.is_transfer:
            if profile.kind == ConnectionKind.SFTP and not sftp_client.driver_available():
                QMessageBox.critical(
                    self,
                    "SSH library missing",
                    "This build has no SSH library, so SFTP tabs cannot connect. "
                    "Install Paramiko and restart.",
                )
                return None
            tab = FileManagerTab(profile, dark_mode=dark, settings=self._settings)
            tab.shell_requested.connect(self._open_shell_tab)
            tab.profile_changed.connect(self._on_profile_changed)
            return tab
        # A browser tab's "dark mode" means the page, not the app chrome.
        return BrowserTab(profile, dark_mode=self._settings.web_dark_mode)

    def _on_profile_changed(self, profile: object) -> None:
        """A tab changed something worth keeping - write it to the vault."""
        if not isinstance(profile, ServerProfile):
            return
        self._store.update(profile)

    def _open_shell_tab(self, profile: object, spec: object, cwd: str) -> None:
        """Open an SSH shell beside the file manager that asked for it."""
        assert isinstance(profile, ServerProfile)
        tab = SshTerminalTab(
            profile, spec, cwd, dark_mode=self._settings.dark_mode
        )
        pane = self._pane()
        index = pane.addTab(tab, f"{profile.label} — shell")
        self._install_close_button(pane, index)
        self._style_tab(pane, index, profile)
        pane.setCurrentIndex(index)
        self._connect_tab_signals(tab)

    def _open_tab(self, profile: ServerProfile) -> None:
        tab = self._build_tab(profile)
        if tab is None:
            return
        if isinstance(tab, BrowserTab):
            self._register_profile(tab.engine_profile)
            self._attach_downloads(tab)
        pane = self._pane()
        index = pane.addTab(tab, profile.label)
        self._install_close_button(pane, index)
        self._style_tab(pane, index, profile)
        pane.setCurrentIndex(index)
        self._connect_tab_signals(tab)

    def _connect_tab_signals(self, tab: QWidget) -> None:
        if hasattr(tab, "status_message"):
            tab.status_message.connect(self._on_tab_status)
        if hasattr(tab, "title_changed"):
            tab.title_changed.connect(lambda title, t=tab: self._update_tab_title(t, title))

    def _clone_current_tab(self) -> None:
        tab = self._pane().currentWidget()
        if not isinstance(tab, BrowserTab):
            self.statusBar().showMessage(
                "Only phpMyAdmin tabs can be cloned into a shared session", 4000
            )
            return
        source_profile = tab.server_profile
        clone = BrowserTab(
            source_profile,
            dark_mode=self._settings.web_dark_mode,
            shared_profile=tab.engine_profile,
        )
        self._register_profile(clone.engine_profile)
        self._attach_downloads(clone)
        pane = self._pane()
        index = pane.addTab(clone, f"{source_profile.label} (clone)")
        self._install_close_button(pane, index)
        self._style_tab(pane, index, source_profile)
        pane.setCurrentIndex(index)
        self._connect_tab_signals(clone)

    def _install_close_button(self, pane: QTabWidget, index: int) -> None:
        """A small, quiet × on the tab, instead of Fusion's red cross."""
        button = QToolButton()
        button.setObjectName("tabclose")
        button.setText("✕")
        button.setToolTip("Close this tab (Ctrl+W)")
        button.setAutoRaise(True)
        button.setFixedSize(16, 16)
        widget = pane.widget(index)
        button.clicked.connect(
            lambda _checked=False, p=pane, w=widget: self._close_widget(p, w)
        )
        pane.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, button
        )

    def _close_widget(self, pane: QTabWidget, widget: QWidget) -> None:
        """Close a tab by identity - its index moves when tabs are dragged."""
        index = pane.indexOf(widget)
        if index >= 0:
            self._on_tab_close(pane, index)

    def _style_tab(self, pane: QTabWidget, index: int, profile: ServerProfile) -> None:
        pane.setTabIcon(
            index, theme.kind_icon(profile.kind.value, self._settings.dark_mode)
        )
        color = _ENV_COLORS.get(profile.environment)
        if color is not None:
            pane.tabBar().setTabTextColor(index, color)
            pane.setTabText(index, f"● {pane.tabText(index)}")
            if profile.environment == Environment.PROD:
                pane.setTabToolTip(index, "PRODUCTION — be careful!")

    def _update_tab_title(self, tab: QWidget, title: str) -> None:
        for pane, index, widget in self._all_tabs():
            if widget is tab:
                short = title if len(title) <= 24 else title[:21] + "…"
                environment = getattr(
                    getattr(tab, "server_profile", None), "environment", None
                )
                prefix = "● " if environment in _ENV_COLORS else ""
                pane.setTabText(index, prefix + short)
                pane.setTabToolTip(index, title)
                return

    def _on_tab_status(self, message: str) -> None:
        if message:
            self.statusBar().showMessage(message, 4000)

    # ----- tab navigation ------------------------------------------------
    def _cycle_tab(self, delta: int) -> None:
        pane = self._pane()
        count = pane.count()
        if count == 0:
            return
        pane.setCurrentIndex((pane.currentIndex() + delta) % count)

    def _goto_tab(self, index: int) -> None:
        pane = self._pane()
        if 0 <= index < pane.count():
            pane.setCurrentIndex(index)

    def _close_current_tab(self) -> None:
        pane = self._pane()
        index = pane.currentIndex()
        if index >= 0:
            self._on_tab_close(pane, index)

    def _on_tab_close(self, pane: QTabWidget, index: int) -> None:
        widget = pane.widget(index)
        pane.removeTab(index)
        if widget is None:
            return
        engine_profile = None
        if isinstance(widget, BrowserTab):
            engine_profile = widget.engine_profile
        # Every tab kind gets a chance to shut its connection/thread down.
        cleanup = getattr(widget, "cleanup", None)
        if callable(cleanup):
            cleanup()
        if engine_profile is not None:
            self._release_profile(engine_profile)
        widget.deleteLater()

    # ----- engine profile lifetime --------------------------------------
    def _register_profile(self, profile) -> None:
        self._profile_refs[profile] = self._profile_refs.get(profile, 0) + 1

    def _release_profile(self, profile) -> None:
        remaining = self._profile_refs.get(profile, 0) - 1
        if remaining <= 0:
            self._profile_refs.pop(profile, None)
            # Drop the in-memory profile (and its cookies) now that the last
            # tab using it is gone.
            profile.deleteLater()
        else:
            self._profile_refs[profile] = remaining

    # ----- sidebar -------------------------------------------------------
    def _set_sidebar_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        """Collapse to the rail, or expand back to the remembered width."""
        if collapsed and self._sidebar.isVisible():
            # Remember how wide it was before it disappears.
            width = self._splitter.sizes()[0]
            if width >= MIN_SIDEBAR_WIDTH:
                self._settings.sidebar_width = width
        self._sidebar.setVisible(not collapsed)
        self._rail.setVisible(collapsed)
        # Give the strip its exact width back, or restore the saved one.
        total = sum(self._splitter.sizes()) or self.width()
        first = _RAIL_WIDTH if collapsed else self._settings.sidebar_width
        self._splitter.setSizes([first, max(total - first, 1)])
        self._collapse_action.setChecked(collapsed)
        if persist:
            self._settings.sidebar_collapsed = collapsed
            self._settings.save()

    def _set_sidebar_hidden(self, hidden: bool, *, persist: bool = True) -> None:
        """Hide the sidebar and its rail entirely (keyboard-only zen mode)."""
        self._sidebar_host.setVisible(not hidden)
        self._sidebar_action.setChecked(hidden)
        if persist:
            self._settings.sidebar_visible = not hidden
            self._settings.save()

    def _on_splitter_moved(self, position: int, index: int) -> None:
        """Remember a width the user dragged; snap to the rail when tiny."""
        if index != 1 or not self._sidebar.isVisible():
            return
        if position < MIN_SIDEBAR_WIDTH:
            self._set_sidebar_collapsed(True)
            return
        self._settings.sidebar_width = position

    # ----- Claude / MCP ----------------------------------------------------
    def _show_mcp_hint(self) -> None:
        """Hand these connections to Claude - said inside the app, because the
        app is where anyone would go looking for it."""
        from mysql_runner.ui.mcp_dialog import MCPDialog

        MCPDialog(self._store.all(), self).exec()

    def _toggle_dark_mode(self) -> None:
        """The application's own chrome."""
        enabled = self._dark_action.isChecked()
        self._settings.dark_mode = enabled
        self._settings.save()
        self._apply_dark_mode_to_tabs(enabled)

    def _toggle_web_dark_mode(self) -> None:
        """Dark mode injected into phpMyAdmin pages - a different thing."""
        enabled = self._web_dark_action.isChecked()
        self._settings.web_dark_mode = enabled
        self._settings.save()
        self._apply_web_dark_mode(enabled)
        self.statusBar().showMessage(
            "phpMyAdmin pages: dark" if enabled else "phpMyAdmin pages: light", 4000
        )

    def _apply_dark_mode_to_tabs(self, enabled: bool) -> None:
        """Restyle the app. Browser tabs keep their own (web) setting."""
        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(theme.app_stylesheet(enabled))
        # Painted icons carry the old theme's colours; draw them afresh.
        self._refresh_server_list()
        for pane, index, widget in self._all_tabs():
            profile = getattr(widget, "server_profile", None)
            if isinstance(profile, ServerProfile):
                pane.setTabIcon(
                    index, theme.kind_icon(profile.kind.value, enabled)
                )
            if isinstance(widget, BrowserTab):
                continue
            setter = getattr(widget, "set_dark_mode", None)
            if callable(setter):
                setter(enabled)

    def _apply_web_dark_mode(self, enabled: bool) -> None:
        for _pane, _index, widget in self._all_tabs():
            if isinstance(widget, BrowserTab):
                widget.set_dark_mode(enabled)

    # ----- settings ------------------------------------------------------
    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self._settings, self, on_change_password=self._change_master_password
        )
        if not dialog.exec():
            return

        if dialog.protection_changed():
            self._apply_protection_change(dialog.protection_mode())

        if dialog.sidebar_visible() != self._settings.sidebar_visible:
            self._set_sidebar_hidden(not dialog.sidebar_visible(), persist=False)
            self._settings.sidebar_visible = dialog.sidebar_visible()

        if dialog.split_view() != (len(self._panes) == 2):
            self._set_split_view(dialog.split_view(), persist=False)
            self._settings.split_view = dialog.split_view()

        if dialog.dark_mode() != self._settings.dark_mode:
            self._settings.dark_mode = dialog.dark_mode()
            self._dark_action.setChecked(self._settings.dark_mode)
            self._apply_dark_mode_to_tabs(self._settings.dark_mode)

        if dialog.web_dark_mode() != self._settings.web_dark_mode:
            self._settings.web_dark_mode = dialog.web_dark_mode()
            self._web_dark_action.setChecked(self._settings.web_dark_mode)
            self._apply_web_dark_mode(self._settings.web_dark_mode)

        self._settings.idle_lock_minutes = dialog.idle_lock_minutes()
        self._settings.ask_password_on_start = dialog.ask_password_on_start()
        self._settings.remember_password = dialog.remember_password()
        self._settings.stay_logged_in = dialog.stay_logged_in()
        # These three only mean something when there is a password to ask for,
        # and the dialog's (disabled) checkboxes still report their old state -
        # so normalise here, after the assignments above, rather than letting a
        # stale tick survive a switch to password-free mode.
        if not vault_mod.requires_password():
            self._settings.ask_password_on_start = False
            self._settings.remember_password = False
            self._settings.stay_logged_in = False
        self._apply_transfer_settings(dialog)
        self._settings.save()
        self._prune_history()

        # Let the app re-arm the idle watcher with the new timeout.
        if self._on_settings_changed is not None:
            self._on_settings_changed()

    def _apply_transfer_settings(self, dialog) -> None:
        """Copy the file-transfer preferences across and tell open tabs."""
        settings = self._settings
        settings.transfer_workers = dialog.transfer_workers()
        settings.atomic_uploads = dialog.atomic_uploads()
        settings.shadow_backups = dialog.shadow_backups()
        settings.history_days = dialog.history_days()
        settings.verify_uploads = dialog.verify_uploads()
        settings.preserve_times = dialog.preserve_times()
        settings.use_ignore_rules = dialog.use_ignore_rules()
        settings.ignore_defaults = dialog.ignore_defaults()
        settings.folder_stats = dialog.folder_stats()
        settings.sync_compare_hashes = dialog.sync_compare_hashes()
        settings.mirror_navigation = dialog.mirror_navigation()
        if dialog.production_guard() and not settings.production_guard:
            # Switching the guard back on is the way back for connections that
            # turned their own warning off; otherwise it would come back on
            # everywhere except where it had actually been silenced.
            settings.production_guard_off = []
        settings.production_guard = dialog.production_guard()
        settings.watch_autosync = dialog.watch_autosync()
        settings.terminal_program = dialog.terminal_program()
        settings.terminal_send_password = dialog.terminal_send_password()
        for _pane, _index, widget in self._all_tabs():
            apply = getattr(widget, "apply_settings", None)
            if callable(apply):
                apply(settings)

    def _prune_history(self) -> None:
        """Enforce the shadow-backup retention the user chose."""
        from mysql_runner.transfer.history import HistoryStore

        days = self._settings.history_days
        try:
            if days <= 0:
                HistoryStore().clear()
            else:
                HistoryStore().prune(max_age_days=days)
        except OSError:
            pass  # Housekeeping; never worth interrupting the user for.

    # ----- vault protection ----------------------------------------------
    def _apply_protection_change(self, target: str) -> None:
        if target == vault_mod.PROTECTION_WINDOWS:
            self._turn_password_off()
        else:
            self._turn_password_on()

    def _turn_password_off(self) -> None:
        confirm = QMessageBox.warning(
            self,
            "Turn off password protection",
            "Your saved connections will stay encrypted, but the key will be "
            "sealed to this Windows account instead of a password — anyone who "
            "can log in as you will be able to open them.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        dialog = UnlockDialog(self)
        dialog.setWindowTitle("Confirm master password")
        if not dialog.exec():
            return
        try:
            vault_mod.disable_password(dialog.password())
        except vault_mod.InvalidMasterPassword:
            QMessageBox.warning(
                self, "Incorrect password", "That master password is incorrect."
            )
            return
        except vault_mod.VaultError as exc:
            QMessageBox.critical(self, "Could not change protection", str(exc))
            return
        # The prompt-related preferences are normalised by the caller once the
        # rest of the dialog's values have been applied.
        QMessageBox.information(
            self,
            "Password protection off",
            "Sitekeeper will no longer ask for a master password on this "
            "Windows account.",
        )

    def _turn_password_on(self) -> None:
        dialog = CreateMasterPasswordDialog(self)
        dialog.setWindowTitle("Choose a master password")
        if not dialog.exec():
            return
        try:
            vault_mod.enable_password(dialog.password())
        except vault_mod.VaultError as exc:
            QMessageBox.critical(self, "Could not change protection", str(exc))
            return
        QMessageBox.information(
            self,
            "Password protection on",
            "Your vault is now protected by a master password.",
        )

    def _change_master_password(self, parent) -> None:
        dialog = ChangeMasterPasswordDialog(parent)
        if not dialog.exec():
            return
        try:
            vault_mod.change_master_password(
                dialog.current_password(), dialog.new_password()
            )
        except vault_mod.InvalidMasterPassword:
            QMessageBox.warning(
                parent, "Incorrect password", "The current master password is incorrect."
            )
            return
        except vault_mod.VaultError as exc:
            QMessageBox.critical(parent, "Could not change password", str(exc))
            return
        QMessageBox.information(
            parent, "Password changed", "Your master password has been updated."
        )

    # ----- downloads -----------------------------------------------------
    def _attach_downloads(self, tab: BrowserTab) -> None:
        # Connect once per engine profile. Clones share an already-connected
        # profile, so only the tab that owns the profile wires the handler.
        if tab.owns_profile:
            tab.engine_profile.downloadRequested.connect(self._on_download_requested)

    def _on_download_requested(self, download) -> None:
        suggested = download.downloadFileName() or "download"
        target, _ = QFileDialog.getSaveFileName(self, "Save download", suggested)
        if not target:
            download.cancel()
            return
        directory, filename = os.path.split(target)
        if directory:
            download.setDownloadDirectory(directory)
        download.setDownloadFileName(filename or suggested)
        download.accept()
        name = filename or suggested
        self.statusBar().showMessage(f"Downloading {name}…")
        download.isFinishedChanged.connect(
            lambda d=download, n=name: self._on_download_finished(d, n)
        )

    def _on_download_finished(self, download, name: str) -> None:
        states = type(download).DownloadState
        state = download.state()
        if state == states.DownloadCompleted:
            self.statusBar().showMessage(f"Saved {name}", 6000)
            path = os.path.join(
                download.downloadDirectory(), download.downloadFileName()
            )
            resp = QMessageBox.question(
                self, "Download complete", f"Saved “{name}”.\n\nOpen it now?"
            )
            if resp == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif state == states.DownloadInterrupted:
            message = f"Download of “{name}” failed."
            reason = download.interruptReasonString()
            if reason:
                message += f"\n\n{reason}"
            QMessageBox.warning(self, "Download failed", message)
            self.statusBar().showMessage(f"Download failed: {name}", 6000)

    # ----- shutdown ------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Stop worker threads before the window goes away."""
        for pane in self._panes:
            for index in reversed(range(pane.count())):
                widget = pane.widget(index)
                cleanup = getattr(widget, "cleanup", None)
                if callable(cleanup):
                    cleanup()
        super().closeEvent(event)

    # ----- lock ----------------------------------------------------------
    def _on_lock_clicked(self) -> None:
        if self._on_lock is not None:
            self._on_lock()


def _startup_message() -> str:
    """"Ready", or the one warning worth giving before anything is opened.

    Running elevated makes every mapped network drive vanish from this
    process while Explorer still shows it - which reads as "the app cannot
    see my share any more". Said here, it is a five-second fix.
    """
    if not running_elevated():
        return "Ready"
    return (
        "Ready — but Sitekeeper is running as administrator, so Windows hides "
        "mapped network drives (Z:, Y:…) from it. Start it normally to browse "
        "them, or use \\\\server\\share paths."
    )


def _category_of(profile: ServerProfile) -> str:
    """The heading a connection belongs under."""
    return profile.group.strip() or _CATEGORY_OF.get(profile.kind, "Other")


def _category_sort_key(name: str) -> tuple[int, str]:
    """Default categories in their fixed order, named groups after them."""
    return (_CATEGORY_ORDER.get(name, len(_CATEGORY_ORDER)), name.lower())
