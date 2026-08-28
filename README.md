# Sitekeeper

> Keeps your sites in order. Save a server once, then open it as an
> auto-logging-in **phpMyAdmin** tab, a native **MySQL console**, or a
> WinSCP-style **FTP / FTPS / SFTP** pane that can keep a folder deployed on
> every save - or every git commit.

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white">
  <img alt="PyQt6" src="https://img.shields.io/badge/GUI-PyQt6-41cd52?logo=qt&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

Save a connection's address, username and password once - encrypted locally - then
double-click it to open. What opens depends on the connection type you picked:

| Type | What you get |
| ---- | ------------ |
| **phpMyAdmin** | A browser tab that fills in the login form for you and keeps its own isolated session. |
| **MySQL** | A real `mysql>` prompt talking straight to port 3306: ASCII result tables, multi-line statements, history, `\G` vertical output. No phpMyAdmin involved. |
| **SFTP / FTP / FTPS** | A dual-pane file manager - local on the left, remote on the right - with recursive upload/download, drag and drop between the panes, hash comparison, and folders that keep themselves deployed on every save or every git commit. |

Open as many as you like at once, in tabs, and put two of them side by side.

<sub>Formerly **MySQL Runner**. It stopped being only about MySQL a few releases
ago; the name caught up in 1.3.0. Your saved connections come across on first
launch - see [Upgrading from MySQL Runner](#upgrading-from-mysql-runner).</sub>

---

## Features

- **Encrypted credential vault** - a random Data Encryption Key protects every credential.
  The key is derived from your **master password** (PBKDF2-HMAC-SHA256) and cached in the
  **Windows Credential Manager**, so you rarely have to retype it. Nothing is ever written
  to disk in plaintext.
- **Connection list with real categories** - connections are filed by what they are:
  **phpMyAdmin**, **MySQL** and **Other (FTP/SFTP)**, each heading carrying its count, and
  empty categories are never drawn. Give a connection a group of its own (Production,
  Client A...) and that wins instead; *Move to a group* on the right-click menu offers the
  groups you already use. The search box matches hostnames as well as labels, so
  `webhosting.cloudcore.cz` finds the account without your remembering what you called it.
  Right-click also connects, edits, duplicates (credentials and all) and deletes.
- **Auto-login** - fills and submits the phpMyAdmin cookie login form and answers HTTP
  Basic Auth prompts. Auth mode is auto-detected or can be forced per server.
- **Per-tab session isolation** - each tab gets its own in-memory cookie jar, so the same
  server can be open in two tabs without sharing a session. Closing a tab discards its cookies.
- **Session cloning** (`Ctrl+D`) - duplicate the current tab to get a second view into the
  **same** logged-in session (shared cookie jar) - inspect a table in one tab while you run a
  query in another.
- **Dark mode, twice over** - the app is dark by default, and the two meanings are separate
  switches because they are separate things:
  - **Dark app theme** (`Ctrl+Shift+D`) - the window, tabs, tables, toolbars and dialogs, from
    one palette in `ui/theme.py`.
  - **Dark phpMyAdmin pages** (`Ctrl+Shift+W`) - themes the phpMyAdmin page itself with the
    bundled [Dark Reader](https://darkreader.org) engine, which computes proper dark colours
    per element (text, backgrounds, borders, images) instead of a flat invert filter.
- **Environment badges** - mark servers as Dev / Staging / **Production**; production tabs
  get a red dot and tint so you never run a destructive query on the wrong server.
- **Startup SQL** - optionally run a query automatically after login (e.g. `SET NAMES utf8;`).
- **Keyboard-driven "Zen Mode"** - hide the sidebar and drive everything from the keyboard.
- **Auto-lock on idle** - after 15 minutes of inactivity (configurable) the key is wiped
  from memory and the keyring cache is cleared. Step away safely.
- **Portable export / import** - export your connections to an encrypted `.mrx` file
  protected by a passphrase, and import them on another PC.
- **Native SQL console** - connect straight to MySQL (port 3306) and get the command-line
  client's behaviour inside a tab: bordered result tables, `Empty set` / `Query OK` summaries
  with timings, multi-line statements ending in `;`, `\G` for vertical output, arrow-key
  history, and the `\c` / `\s` / `\r` / `\q` backslash commands. Queries run on a worker
  thread so the window never freezes, and oversized result sets are capped rather than
  swallowing your memory.
- **Dual-pane file transfers** - SFTP, FTP and FTPS connections open a WinSCP-style two-pane
  view. Browse both sides, transfer whole folders in either direction (recursively), create
  directories, rename and delete, and watch a per-file progress bar you can cancel. SFTP
  checks host keys on a trust-on-first-use basis. In detail:
  - **Several files at once** - the queue runs on six separate connections by default
    (configurable, up to 16), which is the difference between minutes and seconds on a tree
    of small files. A deploy of small files is limited by *round trips*, not bandwidth -
    each file costs an open, a write, a close and a rename however small it is - so the time
    divides almost exactly by the number of connections. Ask for more than the server
    allows and Sitekeeper finds the ceiling and settles there instead of failing the
    transfers into it. Browsing keeps its own connection, so a running queue never blocks
    the panes.
  - **One obvious action at a time** - Upload and Download sit in the same place always,
    and whichever pane you are working in decides which of them is *the* action: that one
    is filled blue and says what pressing it would do ("▲ Upload 12", with the destination
    named), the other stays quiet. The server pane carries the same blue on its edge, so
    the button and the folder it feeds are visibly a pair - red instead on a production
    connection. A pill in the corner is amber while connecting, green when connected, red
    when not.
  - **"Start here next time"** - right-click any folder in either pane to make it where
    this connection opens. The two sides are remembered separately.
  - **A controllable queue** - pause and resume mid-file, cancel one item or all of them,
    drag rows into the order you want, or push one to the front.
  - **Safe deploys** - uploads land on a scratch name and are renamed into place (atomically,
    where the server supports `posix-rename`), so a live request never sees a half-written
    file. Uploaded files are given your local modified date, which can be switched off in
    *Settings ▸ Transfers* - it costs a round trip per file, and syncs compare content
    rather than timestamps now, so it is only about how the dates read on the server.
  - **Undo an overwrite** - whatever a transfer is about to replace is copied into a local
    cache first, and "Undo replace" (`Ctrl+Z`) puts it back. The History window lists
    everything that was overwritten, with a Restore button per entry. Keeping the previous
    version means *downloading* it before the new one goes up, which roughly doubles the
    time a redeploy takes; it is worth having, and worth knowing about before a large one
    (*Settings ▸ Transfers*).
  - **Compare by hash** (`F9`, *Sync ▾*, or either pane's context menu) - digests both
    sides, rolls folders up from their contents, and marks every row `=`, `≠`, `→` or `←`.
    The result window can upload or download exactly the files you tick. On SFTP the
    hashing happens on the server.
  - **Honest folder dates and sizes** - a folder shows the newest timestamp anywhere below it
    and the real total size, instead of the server's own (meaningless) directory mtime.
    Sorting by *Modified* asks for that walk even where the automatic pass is off, so
    folders - which stay above files - are ordered by a date worth ordering by.
  - **Deploy-ignore rules** - `.deployignore` or `.gitignore`, full gitignore syntax, plus a
    built-in list (`node_modules`, `vendor`, `.git`, caches, `.env`). Applied to batch
    transfers, comparisons and the watcher. Right-click a file or folder ▸ *Never deploy*
    writes the rule for you, anchored to that exact path (a folder's rule takes everything
    below it), into the `.deployignore` your transfers actually read - and anything already
    watching re-reads it at once. `.gitignore` is never written to: what you deploy is not
    what git tracks.
  - **Watch a folder** - the tab notices files as your editor saves them, and can upload each
    one straight away. A file is only sent once its size and timestamp have settled, so a
    half-written save never goes up.
  - **Synced folders** - right-click a local folder ▸ *Sync folder* and pick when it should
    reconcile itself: **on save**, so every file goes up as your editor writes it, or
    **on git commit**, where the folder waits for the repository to record a commit and then
    compares the whole tree with the server and uploads what differs. A commit is the honest
    signal for a deploy: it means "this tree is the one I want live", not "this one file
    changed". Rules are remembered per connection and armed again next time you open the tab,
    synced folders are marked ⟳ in the pane, and *Synced folders…* lists everything with its
    trigger. `Ctrl+Shift+S` syncs the folder you are looking at once, without arming it.
    A rule can cover the folder **with its subfolders**, or **only the files sitting in
    it** - which is how a site root gets synced without dragging every subfolder up with
    it. Sync the root that way and the loose files at the top (`index.php`, `.env`) are
    kept up to date while `assets/`, `includes/` and the rest are left to their own rules;
    arm a folder that already has synced folders under it and it picks files-only by
    itself. Toggle *Include subfolders* in the Sync menu, or the **Subfolders** column in
    *Synced folders…*.
    Removals are mirrored - deleting a file locally deletes it on the server - but only ever
    inside a folder that has a rule, and never below its scope, and a full sync shows you
    what only the server has before touching it (put `uploads/`, `logs/` and the like in `.deployignore` and no sync
    will go near them). A comparison is by **content**, not by timestamp: a modified time
    says when git wrote the file, not when its contents were written, so after a clone, a
    pull or a checkout - or with two people deploying one repository from two machines -
    identical files look newer than the copies already on the server and every sync
    re-uploads the whole tree. On a server with a shell the whole remote side is hashed by
    one command. *Settings ▸ Transfers* has the old size-and-timestamp behaviour for the
    case where hashing is too slow and the timestamps can be trusted.
  - **Publish from git history** - *Sync ▾ ▸ Git history…* reads the repository's log and
    sends any file **as it was at any commit**: what one commit changed, for putting a
    single file back the way it was before a bad change, or every file at that commit, for
    rolling a folder back to a known-good release. Nothing is checked out to do it - the
    old bytes are extracted to a scratch folder and uploaded from there, so HEAD never
    moves and your working copy is untouched.
  - **Drag and drop, both ways** - drag rows from one pane to the other to copy them, and drop
    them **on a folder** to land inside it rather than in whichever directory is open; the
    target folder tints as you hover. Hold a drag near the top or bottom edge and the
    listing scrolls itself, so the folder you want does not have to be on screen when you
    pick the files up. Local files can also be dragged out to Explorer or an editor, and
    dropped in from either.
  - **Back, Forward and a recent list** per pane, plus **mirrored navigation** that keeps
    both sides on matching directories.
  - **Permissions** - presets (755 / 644 / 400 / 775 …), a checkbox grid and an octal box that
    stay in step, recursive with a files-only or folders-only scope, and a warning before
    anything world-writable.
  - **Symlinks** - shown in italics with their target, navigable, and retargetable from the
    context menu (which is how you switch which release is live).
  - **Production guard** - on a connection marked production, uploads, deletes, symlink
    changes, commands and watcher syncs all ask first.
- **Server-side tools (SFTP)** - anything that would mean downloading a codebase is done on
  the server instead. Archives (`tar.gz`, `tar.bz2`, `tar`, `zip`) created and unpacked in
  place; content search via ripgrep or grep; an ncdu-style disk-usage view you can walk into;
  a `tail -f` log viewer with a filter; an embedded SSH shell that opens in the directory you
  are looking at (`Ctrl+T`); a one-command runner (`Ctrl+P`); a library of parameterised
  snippets; and a button that hands the session to PuTTY, Windows Terminal, ssh.exe or WSL.
  The app probes once at connect time, so accounts that are SFTP-only simply do not show
  these buttons rather than failing when pressed.
- **Adopt an existing WinSCP install** - File ▸ *Import from WinSCP or a URL list* finds your
  sessions where WinSCP actually keeps them: the registry for an installed copy, or
  `WinSCP.ini` for a portable one, decoding the stored passwords either way. If neither is
  there it asks for a file, which can also be a plain list of
  `sftp://user:pass@host:port/path` strings. *Add from a connection string* takes one pasted
  URL; *Export for WinSCP* writes either format back out.
- **Split view** (`Ctrl+Alt+S`) - two tab panes side by side, so you can watch a query in one
  and a file listing in the other. `Ctrl+Alt+M` throws the current tab to the other pane.
- **Collapsible sidebar** (`Ctrl+B`) - collapse it to a slim rail that keeps an expand button
  in reach, or hide it outright with `Ctrl+Shift+B`. Its width is remembered, and dragging the
  divider almost shut snaps it to the rail.
- **Optional master password** - prefer no prompt at all? Turn password protection off in
  Settings and the encryption key is sealed to your Windows account with DPAPI instead. Your
  connections stay encrypted on disk; you just stop being asked. Switch back whenever you
  like - the stored servers survive either way.
- **Tabbed multi-server workflow** - work with many servers at the same time.

---

## Keyboard shortcuts

| Shortcut            | Action                          |
| ------------------- | ------------------------------- |
| `Ctrl+B`            | Collapse / expand the sidebar   |
| `Ctrl+Shift+B`      | Hide the sidebar entirely       |
| `Ctrl+Alt+S`        | Toggle split view               |
| `Ctrl+Alt+M`        | Move the tab to the other pane  |
| `Ctrl+Alt+Tab`      | Switch to the other pane        |
| `Ctrl+Shift+D`      | Toggle the dark app theme       |
| `Ctrl+Shift+W`      | Toggle dark phpMyAdmin pages    |
| `Ctrl+W`            | Close the current tab           |
| `Ctrl+D`            | Clone the current tab (session) |
| `Ctrl+Tab`          | Next tab                        |
| `Ctrl+Shift+Tab`    | Previous tab                    |
| `Ctrl+1` … `Ctrl+9` | Jump to tab 1–9                 |
| `Ctrl+L`            | Clear the SQL console screen     |

In a file-transfer tab:

| Shortcut         | Action                                    |
| ---------------- | ----------------------------------------- |
| `Alt+←` / `Alt+→`| Back / Forward in that pane's history     |
| `Alt+↑`          | Parent directory                          |
| `F5`             | Refresh the active pane                   |
| `F9`             | Compare both sides by hash                |
| `Ctrl+Z`         | Undo the last overwrite                   |
| `Ctrl+Shift+Q`   | Show / hide the transfer queue            |
| `Ctrl+Shift+S`   | Sync this local folder with the server     |
| `Ctrl+T`         | Open a shell here (SFTP)                  |
| `Ctrl+P`         | Run one command here (SFTP)               |
| `Ctrl+Shift+F`   | Search file contents on the server (SFTP) |

---

## Getting started

Requires **Windows** and **Python 3.10+**.

```powershell
git clone https://github.com/<your-username>/mysql-runner.git
cd mysql-runner
python -m pip install -r requirements.txt
python main.py
```

On first launch you choose a **master password** - or tick *Don't use a master password*
to have the key sealed to your Windows account instead. Then:

1. Click **Add**, pick the connection **type** (phpMyAdmin, MySQL, SFTP, FTP, FTPS) and
   fill in the fields it asks for. Optionally set a group, environment level and startup SQL.
2. Double-click the connection (or select it and press **Connect**) to open it.
3. Use **File → Export / Import** to move your connections between machines.

---

## Letting Claude use your servers (MCP)

Sitekeeper ships an [MCP](https://modelcontextprotocol.io/) server, so Claude Code and
Claude Desktop can browse, deploy to and query the same servers the app manages -
against the same encrypted vault, with nothing re-configured. Register it once:

```powershell
cd mysql-runner
claude mcp add sitekeeper -- python -m mysql_runner.mcp --allow-write
```

Claude then gets tools to list your profiles, read and list remote files, download,
upload files and folders (`.deployignore`/`.gitignore` are honoured), and run MySQL
queries with `mysql`-client-style output.

**Everything is read-only until a flag grants more:**

| Flag | Grants |
| --- | --- |
| *(none)* | listings, file reads, downloads, `SELECT`-style SQL |
| `--allow-write` | uploads and creating remote directories |
| `--allow-delete` | deleting remote files and directories |
| `--allow-sql-write` | SQL that changes data |
| `--allow-production` | lets the flags above touch profiles marked **PROD** |
| `--profiles "A,B"` | restricts the server to the named profiles |

The vault unlocks the way the app does: a password-free vault opens via Windows data
protection, a password vault via the key cached at your last unlock (or the
`SITEKEEPER_MASTER_PASSWORD` environment variable as a last resort). Credentials are
never exposed through the tools.

---

## Building a standalone `.exe`

A [PyInstaller](https://pyinstaller.org/) spec is included that bundles the Qt WebEngine
runtime:

```powershell
python -m pip install -r requirements.txt
pyinstaller Sitekeeper.spec
```

The executable is produced at `dist/Sitekeeper.exe` - one file, Qt WebEngine and
all. For the release build (UPX-compressed, zipped, wrapped in an installer) see
below.

### Release build and installer

**Bump the version first.** Two builds carrying the same version cannot be told apart
once they are installed - Add/Remove Programs, the exe properties and the setup
filename all read the same. New features take a minor bump, fixes a patch. Four
places carry it:

| File | What to change |
| --- | --- |
| `version_info.txt` | `filevers`, `prodvers`, `FileVersion`, `ProductVersion` |
| `installer\Sitekeeper.nsi` | `APP_VERSION` and `VIProductVersion` |
| `frontend\package.json`, `package-lock.json`, `src-tauri\tauri.conf.json` | the web front end's `version` |
| `CHANGELOG.md` | close `## [Unreleased]` as the new version, with the date |

`installer\build.ps1` reads `APP_VERSION` out of the `.nsi`, so the setup filename
follows on its own - it used to be typed out separately, which is how a 1.0.3
installer once ended up wrapping a 1.1.0 exe.

`build_release.ps1` produces the one-file, UPX-compressed exe and a zip beside it, and
`installer\build.ps1` wraps that exe into an NSIS setup:

```powershell
.\build_release.ps1                  # -> dist_onefile_upx\Sitekeeper.exe + release\*.zip
powershell -ExecutionPolicy Bypass -File installer\build.ps1
                                     # -> installer\Sitekeeper-1.8.0-Setup.exe
```

It builds from the virtual environment at `%USERPROFILE%\.venvs\mysqlrunner`, which needs
**every** runtime dependency, not just PyQt: a venv missing `PyMySQL`, `paramiko` or
`keyring` builds happily and ships an app whose SQL console and SFTP tabs report a
missing driver. `pip install -r requirements.txt` into that venv is the check worth
doing before a release. UPX (`winget install UPX.UPX`) and NSIS are both needed; the
installer script also looks for the NSIS that ships with Tauri.

---

## Project layout

```
main.py                          Entry point
Sitekeeper.spec                 PyInstaller build spec
mysql_runner/
  app.py                         Bootstrap: unlock vault -> main window, idle auto-lock
  paths.py                       Per-user AppData file locations
  crypto/vault.py                DEK/KEK, keyring + master-password, Fernet
  storage/models.py              ServerProfile (groups, environment, startup SQL)
  storage/store.py               Encrypted load/save of profiles
  storage/settings.py            Plain-JSON UI preferences
  storage/portable.py            Passphrase-encrypted export/import (.mrx)
  crypto/dpapi.py                Windows DPAPI sealing for the password-free mode
  ui/main_window.py              Sidebar + rail, split panes, tabs, menus, shortcuts
  ui/server_dialog.py            Add/edit connection (fields follow the chosen type)
  ui/master_password_dialog.py   Set / unlock / change dialogs
  ui/settings_dialog.py          Appearance, split view, vault protection
  ui/idle_watcher.py             Global idle auto-lock timer
  ui/sql_console_tab.py          The mysql> console tab
  ui/file_manager_tab.py         Dual-pane transfer tab
  ui/transfer_queue_panel.py     The queue: pause, reorder, cancel per item
  ui/compare_dialog.py           What differs, and sending it either way
  ui/git_history_dialog.py       The commit log, and publishing out of it
  ui/history_dialog.py           Overwrites, and restoring one
  ui/sync_dialog.py              Synced folders: trigger, scope, removals
  ui/permissions_dialog.py       chmod presets, checkbox grid, octal box
  ui/remote_tools.py             Search, disk usage, archives, commands, snippets
  ui/ssh_terminal_tab.py         Embedded SSH shell, opened in the current directory
  ui/log_viewer.py               Live remote log viewer (tail -f)
  db/mysql_client.py             PyMySQL connection driven on a worker thread
  db/sqlsplit.py                 Statement splitting (quote- and comment-aware)
  db/resultformat.py             ASCII result tables, vertical layout, summaries
  transfer/base.py               RemoteFS interface + capability flags
  transfer/ftp_client.py         FTP / FTPS via ftplib (MLSD, LIST fallback)
  transfer/sftp_client.py        SFTP via Paramiko, trust-on-first-use host keys
  transfer/worker.py             Qt bridge: navigation, queue and tool channels
  transfer/pool.py               Multi-connection transfer queue, atomic uploads
  transfer/hashing.py            File and folder digests, two-sided comparison
  transfer/treestat.py           Recursive folder size and newest-content date
  transfer/ignore.py             .deployignore / .gitignore engine
  transfer/history.py            Shadow backups and the undo journal
  transfer/navhistory.py         Back / forward memory, mirrored navigation
  transfer/remote_exec.py        Server-side archives, grep, du, tail, chmod
  transfer/permissions.py        Octal helpers and the chmod presets
  transfer/snippets.py           Saved snippet library
  transfer/watcher.py            Polling watcher for local edits
  transfer/syncrules.py          Synced folders: local/remote pairs and triggers
  transfer/gitwatch.py           Commit detection from HEAD, refs and the reflog
  transfer/githistory.py         Reading the log, and a file as it was at a commit
  transfer/connstr.py            Connection strings and WinSCP.ini import/export
  transfer/spawn.py              PuTTY / Windows Terminal / ssh.exe launcher
  web/profile_factory.py         Isolated in-memory profile per tab
  web/browser_tab.py             QWebEngineView + auto-login + dark mode + startup SQL
  web/autologin.py               phpMyAdmin login / dark-mode / startup JavaScript
```

---

## Two front ends, one core

The application logic lives in `mysql_runner/` and is driven by either of two
shells:

| | stack | status |
| --- | --- | --- |
| **Desktop (Qt)** | PyQt6 + Qt WebEngine, `python main.py` | complete, ships today |
| **Desktop (web)** | Tauri 2 shell + Svelte 5 UI + the same Python core as a FastAPI sidecar | in progress |

The web front end follows the same shape as RaplMail: the Rust shell picks a
free loopback port and a per-launch token, spawns the frozen Python backend as
a sidecar, and hands both to the webview. Nothing is duplicated - the vault,
the MySQL console, and the FTP/SFTP backends are the same modules the Qt build
uses, exposed over HTTP instead of driven by widgets.

```
mysql_runner/          shared core (vault, models, db/, transfer/)
backend/app/
  main.py              FastAPI app, /health, /events WebSocket
  api/                 vault, servers, sql, transfer routers
  core/                config, vault state, event hub
  services/            non-Qt MySQL + transfer session managers
frontend/
  src/                 Svelte 5 UI (rail, sidebar, console, dual pane)
  src-tauri/           Rust shell: port + token, sidecar spawn, single instance
```

Run the pieces separately while developing:

```powershell
python backend\run.py --port 8766      # API on http://127.0.0.1:8766
cd frontend; npm run dev                # Vite on http://localhost:5173
cd frontend; npm run tauri dev          # the real desktop window
```

`npm run dev` talks to `127.0.0.1:8766` with no token, so the UI can be worked
on without building the Rust side at all.

### What is not ported yet

Synced folders and pane-to-pane drag and drop are Qt-only so far. The engines
behind them (`transfer/syncrules.py`, `transfer/gitwatch.py`) are Qt-free like
the rest of the core, so the web front end needs endpoints and a UI rather than
any new logic.

The phpMyAdmin browser tab. In the Qt build each tab gets its own off-the-record
`QWebEngineProfile`, which is what makes two independent sessions to the same
server possible. Tauri can match that - it keys its webview context by
`data_directory`, so a unique directory per tab gives a separate cookie jar and
a shared one reproduces the `Ctrl+D` clone - but the shell does not create those
webviews yet, so for now a phpMyAdmin connection opens in your default browser.
HTTP Basic Auth has no equivalent at all: wry does not expose WebView2's
`BasicAuthenticationRequested`, so those profiles will keep going out to the
system browser.

---

## Developing from the NAS share

This checkout normally lives on an SMB share. Samba creates files there without
the execute bit, so Windows refuses to launch an `.exe` or load a `.node`/`.dll`
from it - which breaks `npm install` and `vite build` with *Access is denied* or
`ERR_DLOPEN_FAILED`. One command sorts it out:

```powershell
.\dev-setup.ps1 -Verify
```

It installs with `--ignore-scripts` (the postinstall would try to run a binary
that is not yet executable), grants `RX` to each native binary **one file at a
time** - the recursive `icacls /T` form reports success but does not stick on
this share - and points `CARGO_TARGET_DIR` at local disk, because a cargo target
tree on SMB is unbearably slow. Re-run it after any `npm install`.

The durable fix belongs on the NAS. Give the share a create mask that keeps the
execute bit, in `smb.conf`:

```ini
create mask    = 0775
directory mask = 0775
```

With that in place plain `npm install` works and the permission step becomes
unnecessary. The Qt build never needed any of this - Python is interpreted and
never asks for the execute bit.

To produce the sidecar binary the desktop shell spawns:

```powershell
.\build-sidecar.ps1
```

That freezes `backend/` with PyInstaller (on local disk), smoke-tests the
result against `/health`, and copies it to
`frontend/src-tauri/binaries/mysqlrunner-backend-x86_64-pc-windows-msvc.exe`.

---

## Upgrading from MySQL Runner

Nothing to do: launch Sitekeeper once and it takes over what MySQL Runner had.

| What | What happens |
| --- | --- |
| `%APPDATA%\MySQLRunner` (vault, connections, settings, sync rules, known hosts, shadow backups) | Moved to `%APPDATA%\Sitekeeper` the first time it is needed. A rename where the volume allows it, a copy where it does not - in which case the old directory is left where it is rather than risked. |
| The cached vault key in Windows Credential Manager | Read from the old entry once, then written back under the new name. No extra master-password prompt. |
| A vault sealed with DPAPI (*no master password* mode) | Opens unchanged. The DPAPI entropy is part of the encryption, not a label, so it deliberately keeps the old string - changing it would make those vaults unreadable with nothing to fall back on. |
| `.mrx` exports written by MySQL Runner | Still import. New exports carry the new marker; both are accepted. |
| The old install in Add/Remove Programs | The Sitekeeper installer offers to run MySQL Runner's uninstaller first, so you do not end up with both. |

`MYSQLRUNNER_NO_KEYRING` still works alongside the new `SITEKEEPER_NO_KEYRING`;
scripts that set it were protecting a real vault.

---

## Security notes

- Files live under `%APPDATA%\Sitekeeper\` (`vault.json`, `servers.enc`, `settings.json`,
  plus `known_hosts` once you use SFTP).
- Credentials are stored encrypted; the key lives in memory only while the app runs and is
  wiped on **Lock** or idle auto-lock.
- With password protection **on**, the key is derived from your master password and cannot be
  recovered from the files alone. With it **off**, the key is sealed using Windows DPAPI: the
  files stay encrypted and are useless on another account or machine, but anyone who can run
  code as you on this machine can open them. Auto-lock is disabled in that mode, since
  re-unlocking would be instant.
- Exported `.mrx` files are encrypted with a passphrase you choose — keep that passphrase safe.
- SFTP host keys are recorded in `known_hosts` the first time you connect. If the key for a
  known host later changes, the connection is refused rather than trusted; when a server has
  genuinely been rekeyed, delete its line from that file.

---

## License

Released under the MIT License. See [LICENSE](LICENSE).
