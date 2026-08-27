# Changelog

All notable changes to Sitekeeper are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.7.0] - 2026-08-27

### Added
- **Either folder of a sync rule can be changed in place.** In *Synced
  folders*, double-click a **Folder** or **Server folder** cell - or use the
  two new buttons - to point an existing rule somewhere else. The server
  side opens the folder tree at the rule's current target, so the usual
  correction ("this is one level too deep") is a single click on the parent.
  Correcting a rule used to mean *Stop syncing* and arming it again, which
  throws away the trigger, the scope and the removal setting along with the
  mistake - and re-arming quietly kept the old server folder, so the obvious
  fix changed nothing. The local side refuses a folder another rule on the
  same connection already holds, since rules are found by their folder and
  two on one folder would be ambiguous.

### Changed
- **Arming a folder that is already synced now says which server folder it
  keeps.** Arming has never re-pointed an existing rule from the panes - a
  pane that happens to be somewhere else must not silently move a working
  target - but it never mentioned the difference either, so a rule whose
  server folder was wrong looked correctly armed. The status line now names
  the folder the rule keeps, the one the pane is showing, and where to change
  it.

## [1.6.1] - 2026-08-27

### Fixed
- **A sync no longer walks the remote pane off into one of the folders it
  just wrote to.** Every push is split into one batch per sub-directory,
  and each batch was adopted as "the directory the transfers landed in" -
  so when the queue drained, the pane was refreshed into whichever
  sub-directory happened to go up last. An `/admin` in the commit was
  enough to leave the pane sitting in `/admin`. That mattered because the
  destination of the next push is read off the pane: the commit offer
  pairs the two folders on show, and a watched folder uploads relative to
  the remote pane. So the push after that aimed inside the folder the
  previous one had wandered into - `/admin/admin/new_file` - and each one
  nested deeper. Uploads a trigger starts (a save, a commit, a folder
  comparison) now go up without moving the pane, exactly as an
  edit-in-place save already did; when the queue drains, the directory you
  are actually in is the one that gets refreshed. Folders a commit adds
  are still created before the files land, and a watched folder now reads
  its destination once, before the first file goes up, rather than once
  per batch.

## [1.6.0] - 2026-08-27

### Added
- **A Browse button on both path bars.** The local one opens Windows's own
  folder picker. The remote one opens a tree of the server's folders that
  expands a branch at a time and leaves the pane where it was until you press
  *Go here* - so reaching `/var/www/vhosts/example.com/httpdocs` is one dialog
  rather than six listings, each of which used to repaint the pane you were
  working in. The picker reads over a connection of its own, so it still opens
  while a comparison or a folder-size sweep is running.
- **A commit offer can be opened up.** Clicking the notice - or its *What goes
  where…* button, or *Sync ▸ What the last commit would send…* - lists every
  file the push would send with the full server path it lands on, the
  deletions it would carry out, and the files in the commit that are **not**
  going anywhere, each with the reason.

### Changed
- **The commit offer says where it is actually going.** It used to say only
  that it "would upload 3 file(s) under /some/folder", which left the pairing
  to be guessed and made it read as though the commit would go to whichever
  folder happened to be open. It now names both folders - the one it reads
  from and the one it writes to - says outright that the pair is the two panes,
  and counts the files in the commit that fall outside that folder instead of
  dropping them silently. Move either pane and the offer is worked out again
  for the new pair, so the folder named on the strip is always the folder the
  push uses. Accepting it now pushes the commit that is remembered rather than
  one captured when the strip was drawn.
- **The connection sidebar wastes less width.** Its tree used Qt's default
  indent, which is meant for trees that nest deeply; this one is two levels
  deep and every row carries an icon saying what it is, so a third of a narrow
  sidebar was empty space to the left of every connection. Tightened.

## [1.5.2] - 2026-08-27

### Fixed
- **The installer no longer starts a Sitekeeper that cannot see your network
  drives.** Installing needs administrator rights, and *Launch Sitekeeper* on
  the last page started the app from the installer itself - so it inherited
  that elevated token. Windows maps network drives per logon session and
  hides them from elevated programs, so the app came up blind to `Z:`, `Y:`
  and every other mapped share while Explorer still showed them, which reads
  exactly like the app breaking. The finish page now launches through
  Explorer, which runs as you, so the app starts with your own token like a
  Start-menu click does. Anything installed before this: close it and start
  it from the Start menu.
- **And if it happens anyway, the app says so.** Started as administrator by
  hand, the status bar says up front that Windows is hiding mapped drives
  from it, and a folder that cannot be reached for that reason explains it
  instead of reporting a bare "is not a directory" - naming the two ways out
  (start it normally, or use the `\\server\share` path, which works either
  way).

## [1.5.1] - 2026-08-27

### Fixed
- **Pushing a commit from the offer no longer crashes.** The handler still
  referred to a variable that had moved into a helper during 1.5.0's
  refactor, so accepting the offer raised `NameError` the moment the commit
  had anything to upload - which is every time it is worth accepting. The
  ignore rules are now read once and passed where they are needed, and the
  package is linted for undefined names so this class of mistake cannot ship
  again.
- **A dismissed commit offer is no longer lost.** The commit and what it
  touched are remembered, so *Sync ▸ Push the last commit* pushes it however
  long afterwards - previously, once the prompt was gone (or had crashed),
  that commit could not be deployed without a full folder sync.

### Changed
- **The commit offer is a strip above the panes, not a modal dialog.** A
  commit is noticed in the background, and background news must not seize the
  window mid-keystroke and demand an answer before anything else can happen.
  The offer now appears as a dismissible bar with *Push*, *Push every commit*
  and a *Don't ask again* box; browsing, transfers and everything else carry
  on around it, and the hovering tooltip lists the files it would send.
- **The production warning can switch itself off, per connection.** Deploying
  to the same live site all day meant answering the same question dozens of
  times, and a prompt answered by reflex has stopped being a safeguard. The
  warning now carries *Don't ask again for this connection*; other production
  connections keep asking, and turning *Ask before anything destructive on
  production* off and on again in Settings restores it everywhere.

## [1.5.0] - 2026-08-26

### Fixed
- **A commit-triggered sync sends the commit, not the site.** It used to
  re-scan the whole tree against the server on every commit - minutes on a
  large site, and its removal pass kept tripping over files that only ever
  existed on the server (logs, caches, uploads). Now git itself is asked what
  the commit changed (`git diff --name-status`), and exactly those files are
  uploaded - and, when removals are mirrored, exactly the files the commit
  deleted are removed. The full comparison remains the fallback when git
  cannot answer (git not installed, an unknown parent commit), and a
  checkout or reset that would remove more than 25 server files still asks
  first.
- **An expired session reconnects itself.** Idle FTP and SSH connections get
  dropped by servers and firewalls without a word, and the first click after
  a pause used to fail - and keep failing - until the tab was closed. Every
  connection (navigation, transfer pool, tool channel) now notices it has
  died, reconnects, and retries: a browse retries the listing, a queued file
  gets one fresh attempt on a new connection, and a sync scan reopens its
  channel.
- **Mirrored navigation stays alive.** Mirror anchored itself at the first
  pair of directories it ever saw and never re-anchored, so once you browsed
  elsewhere it silently did nothing. Ticking Mirror now anchors at the pair
  on screen, leaving the anchored tree follows you to the new pair, and a
  matching folder that does not exist on the other side says so in the
  status line instead of staying quiet.
- **Sorting no longer wrecks the column layout.** Every sort click re-fitted
  the columns to their content, collapsing whatever widths had been dragged
  out. Columns now hold their widths; the name column takes the remaining
  space.

### Changed
- **The palette is graphite now.** Every grey is a true neutral - the old
  panels had a blue cast - and the blue accent is gone entirely: focus,
  selection and primary actions read as brightness (light graphite on dark,
  dark graphite on light), the way GrapheneOS does it. The only colours left
  are the semantic ones - green, amber, red - plus a muted steel for the
  comparison marks and code-file icons, which used to borrow the accent.
- **Listings sort by modified time, newest first, by default.** On a live
  site "what changed?" is the question a listing answers; click a header for
  any other order, as before.
- **SFTP transfers are much faster on real latency**: the SSH channel window
  is raised from Paramiko's 2 MB default to 16 MB, so the link stays full
  instead of waiting on acknowledgements, and per-file overwrite checks cost
  one round trip instead of two.
- **Re-uploading an unchanged file no longer downloads it first.** The shadow
  backup exists so an overwrite can be undone - but when the server copy has
  the same size and timestamp as the file going up, nothing is lost by
  skipping it, and re-deploys stop paying a full download per file.

### Added
- **Connection icons.** Every connection now carries a glyph for what it is -
  a globe for phpMyAdmin, a database cylinder for MySQL, transfer arrows for
  FTP, and the same arrows with a green padlock for FTPS/SFTP - in the
  sidebar and on every tab. Painted like the rest of the app's icons, so
  they match both themes.
- **Tools → Connect Claude (MCP server)…** explains the Claude integration
  inside the app: the exact `claude mcp add` command with a Copy button,
  where to run it, and what each permission flag grants.
- **Commits offer themselves for pushing, sync rule or not.** The repository
  the local pane is browsing is watched even when no folder sync is set up;
  commit there and the app asks: *Push once*, *Push every commit* (which arms
  an on-commit sync on the spot), or *Not now* - with a "don't ask about this
  folder again" box that is remembered. The offer names exactly what the
  commit would upload and remove, mapped from the local pane to the remote
  one, and folders that already have any sync rule are never nagged.
- **Claude can drive your servers (MCP).** `python -m mysql_runner.mcp`
  starts a Model Context Protocol server - `claude mcp add sitekeeper --
  python -m mysql_runner.mcp` registers it - giving Claude Code and Claude
  Desktop tools to list profiles, browse and read remote files, download,
  upload files and folders (ignore rules honoured), and run MySQL queries
  with mysql-client-style output. It opens the same encrypted vault the app
  uses (Windows-sealed or keyring-cached key; `SITEKEEPER_MASTER_PASSWORD`
  as a fallback) and is read-only by default: uploads need `--allow-write`,
  deletions `--allow-delete`, data-changing SQL `--allow-sql-write`, and
  none of those touch a PROD-marked profile without `--allow-production`.
  `--profiles` restricts which connections it may use at all. See the README.
- **The mouse's Back and Forward buttons navigate the pane under them** -
  back through the visited-directory history (or up a folder when there is
  none), forward again the way a browser does.

## [1.4.0] - 2026-08-24

### Fixed
- **Parallel transfers no longer fail on the undo journal.** The shadow-backup
  journal was written with a fixed temporary name and no lock, so the pool's
  worker threads collided on it - and the collision (`Permission denied` /
  `being used by another process` on `journal.json.tmp`) failed the transfer
  itself. This is also why a commit-triggered sync appeared to upload only one
  file: the rest of the batch died on the journal, not on the server. The
  journal now takes one lock per journal file, every write goes to a uniquely
  named temp file, and even if a write still fails it becomes a note on the
  item instead of a failed transfer.
- **A commit-triggered sync waits out a busy tool channel.** Folder statistics
  on a large directory could hold the channel longer than the 20 seconds the
  sync was willing to retry, and the sync was silently dropped. It now retries
  for a minute.

### Changed
- **FTP transfers use 128 KB blocks** instead of ftplib's 8 KB default, in
  both directions - the same bytes in a sixteenth of the socket calls.

### Added
- **Remote files open for editing in place.** Double-click a remote file (or
  right-click → Edit locally): it is fetched to a scratch folder and opened
  with whatever that file type launches, and from then on every save is
  noticed and uploaded straight back - the download-edit-reupload-delete loop,
  removed. Saves are pushed only once their timestamp holds still, so an
  editor mid-write is never caught half-saved; a file over 20 MB asks first,
  and on a production connection the one question up front covers the saves
  that follow. Double-clicking a *local* file simply opens it, as Explorer
  would.
- **Type-to-filter in both panes** (Ctrl+F): show only names containing what
  you typed, Esc clears, and the filter resets when you change directory.
- **Failed transfers can be retried** - a "Retry failed" button appears in the
  queue whenever something has failed, and single rows retry from their
  right-click menu. Starting a new transfer no longer forgets failures, so
  they stay retryable until cleared.
- **The standard file-manager keys work**: F2 renames, F7 makes a folder, and
  Delete deletes - handled inside the listing itself, so pressing Delete
  while typing in a filter or path box can never mean "delete files".
- **Each pane counts itself**: "3 folder(s), 24 file(s)" beside the title,
  turning into "5 selected — 1.2 MB" while anything is selected.
- **A Sync activity window** (Sync menu → Sync activity…) answers the two
  questions a background sync leaves hanging: did it see my commit, and did
  everything actually go up? Every commit and save a watcher notices becomes
  an entry - the commit's branch, hash and subject line - with the files the
  comparison decided to send underneath, each one tracking its upload live:
  queued, uploading, uploaded, or failed with the reason. Removals, "already
  in step", "waiting for the connection" and the production guard all leave
  their trace too. Events are logged whether or not the window is open, so
  opening it after the fact still shows the whole session.
- **The transfer queue groups each run into a timestamped batch.** A new
  transfer folds the previous batches up into their headline -
  "14:32:05 — 7 file(s)" with "7/7 done" or "1 failed" alongside - so an
  afternoon of deploys stays one screen tall while every failure is still one
  click away. The pool also forgets its finished items when a new run starts,
  so the counters speak about the work at hand; old batches with nothing
  unfinished are dropped once enough newer ones exist.
- **The queue panel is now yours to shape**: its columns can be dragged into
  a different order and resized, and the panel itself sits on a splitter, so
  its height can be dragged down to a sliver instead of being fixed.
- **The path bar is now clickable breadcrumbs.** Each folder in the path is a
  button that jumps back to it; clicking the empty space to the right turns
  the bar back into the familiar line edit for typing or pasting (Enter
  navigates, Escape cancels). Very deep paths collapse their middle into a
  "…" menu.
- **Folder and file icons in both panes.** Painted like the navigation
  glyphs, so remote files - which have no real path for Windows to supply an
  icon for - look exactly like local ones: amber folders, and pages tinted by
  type (code, image, archive). Folders lost their `[brackets]`; the icon says
  it now.
- **Listings sort by any column.** Click Name, Size, Modified or Mode to
  sort, click again to reverse; folders stay above files and ".." stays on
  top either way.

## [1.3.0] - 2026-08-24

### Changed
- **MySQL Runner is now Sitekeeper.** It stopped being only about MySQL several
  releases ago - it manages phpMyAdmin sessions, a native SQL console, FTP/FTPS/
  SFTP transfers, server-side tooling over SSH and folders that deploy
  themselves - and a name that promised one of those was getting in the way. The
  window, the executable, the installer, the Start Menu entry and the data
  directory all follow.
  - **Nothing has to be moved by hand.** `%APPDATA%\MySQLRunner` - the vault,
    the connections, the settings, the sync rules, the known hosts and the
    shadow backups - is taken over on first launch: renamed where the volume
    allows it, copied where it does not, and left alone rather than risked if
    neither works.
  - The cached vault key is read from the old Credential Manager entry once and
    written back under the new name, so the rename costs no extra
    master-password prompt.
  - The **DPAPI entropy deliberately keeps the old string**. It is an input to
    the encryption rather than a label, so a vault sealed in *no master
    password* mode would become unreadable if it changed - there is nothing to
    fall back on. A comment in `crypto/dpapi.py` says so, for whoever tidies
    names next.
  - `.mrx` exports from MySQL Runner still import; new ones carry the new
    marker and both are accepted. `MYSQLRUNNER_NO_KEYRING` is still honoured
    beside the new `SITEKEEPER_NO_KEYRING`.
  - The installer offers to run MySQL Runner's uninstaller first, so a machine
    does not end up carrying both.

### Added
- **A synced folder can cover only the files in it**, instead of everything
  below it. That is what makes a site root syncable: the loose files at the top
  go up and stay up, while `assets/`, `includes/` and the rest are left to the
  rules that own them - or left alone entirely. Arming a folder that already has
  synced folders under it picks files-only by itself and says so, since a root
  over seven synced subfolders is never a request to upload all seven again.
  Toggle it with *Include subfolders* in the Sync menu or the **Subfolders**
  column in *Synced folders…*. The scope is honoured all the way down: the
  watcher does not walk the subfolders, the comparison does not read them, and a
  files-only rule will not remove anything it finds in them.
- **Dragging near a pane's top or bottom edge scrolls the listing**, at about a
  page every third of a second, with the drop target re-highlighted as the rows
  move under the pointer. Qt's own auto-scroll never ran because the panes
  handle their drag events themselves, which left long listings undroppable
  below the fold.

### Fixed
- ***Synced folders…* appeared to do nothing.** The window was opening behind the
  main window: a dialog shown while a popup menu is still closing loses the race
  with Windows re-activating the window underneath, and a dialog parented to a
  widget has no taskbar button to find it by. It is now raised on the next turn
  of the event loop, and reports what it found on the status line.

## [1.2.0] - 2026-08-24

Synced folders: pick a local folder, pick when it should go up - as you save,
or as you commit - and it keeps itself on the server from then on. Plus drag
and drop between the panes, and a connection list that is finally sorted by
what things are.

### Added
- **Synced folders** (`transfer/syncrules.py`, `transfer/gitwatch.py`). Right-click
  a local folder ▸ *Sync folder* and choose when it reconciles itself: **on save**
  (each file goes up as soon as its size and timestamp settle) or **on git commit**
  (the repository records a commit, and the whole folder is compared with the server
  and brought into step). A commit is the honest trigger for a deploy - it means
  "this tree is the one I want live" rather than "this one file changed" - and
  comparing everything catches drift a per-file watcher cannot.
  - Commits are noticed by reading git's own files - `HEAD`, the ref it points at,
    `packed-refs`, and `logs/HEAD` for the subject line - so no `git` binary has to
    be on PATH, no repository lock can block it, and it costs two small file reads a
    second. Any move of HEAD counts: commit, amend, merge, reset, or a checkout of
    another branch.
  - Rules are stored per connection in `sync_rules.json` (two paths and a mode; no
    credentials) and armed again when the tab is reopened. Synced folders are marked
    ⟳ in the local pane, the header button shows how many are live, and *Synced
    folders…* lists every rule with its trigger, its repository and whether it is
    watching. `Ctrl+Shift+S` reconciles the current folder once without arming it.
  - **Removals are mirrored, and fenced in.** A watched deletion is unambiguous, so
    it is mirrored at once; a full sync cannot tell "you deleted this" from "the
    server wrote this", so it lists what only the server has and asks once per
    folder. Either way every path a rule would delete is checked to be inside that
    rule's own remote folder first, missing directories are collapsed to their
    topmost parent, and `.deployignore` keeps `uploads/`, `logs/` and caches out of
    the comparison entirely.
  - Transfers go through the existing multi-connection pool, and a scan compares by
    size and timestamp wherever the server accepts a timestamp on upload (it does,
    over SFTP), falling back to hashes only where timestamps cannot be trusted -
    which is what keeps a commit-triggered sync of a large tree quick. Scans run on
    the read-only tool channel, so a sync never blocks browsing, and one that
    arrives while a comparison is running is re-queued rather than lost.
  - A trigger that fires while the connection is down is remembered and run when it
    comes back. On a production connection the guard asks first, and declining
    pauses the rule instead of dropping it.
- **Drag and drop between the panes.** Rows dragged from one pane to the other are
  uploaded or downloaded, and dropping them **on a folder row** puts them inside
  that folder rather than in whichever directory happens to be open - the row tints
  while a drag hovers over it, and dropping on `..` uses the parent. Local rows
  carry file URLs too, so they can be dragged out to Explorer or an editor; remote
  rows have no real files to offer and so stay in the app. Drops from outside now
  honour the folder they land on as well.
- **The connection list has categories.** Anything without a group of its own is
  filed by what it is - **phpMyAdmin**, **MySQL**, **Other (FTP/SFTP)** - with a
  count per heading, in a fixed order, entries sorted by name, and empty categories
  left undrawn; a connection with a group keeps it, and named groups sort after the
  defaults. One "Ungrouped" heading told you nothing: a phpMyAdmin login and an
  SFTP account are different tools. The search box now matches the target as well
  as the label, so a hostname finds a connection. Right-clicking the list connects,
  edits, duplicates (credentials and all) or deletes a connection, or moves it to a
  group.
- **The file manager grew up.** Everything in `ideas_for_ftp.md` is implemented,
  in a Qt-free core (`mysql_runner/transfer/`) that both front ends use.
  - **Multi-connection transfers** (`transfer/pool.py`). The queue runs on a
    configurable handful of separate connections, which is what turns a tree of
    small files from minutes into seconds. Three channels are kept apart on
    purpose: navigation, the transfer pool, and a third for slow read-only jobs -
    so a running queue never blocks browsing, and a comparison of ten thousand
    files never freezes a pane.
  - **A queue you can control.** Pause and resume *mid-file* (the pause is
    honoured inside the progress callback, not just between files), cancel one
    item or all of them, drag rows to reorder, or push one to the front.
  - **Atomic uploads.** Bytes go to a scratch name and are renamed into place,
    using OpenSSH's `posix-rename` where the server has it, so a live request can
    never be served a half-written file. The local file's timestamp is preserved
    afterwards, which is what keeps later comparisons meaningful.
  - **Shadow backups and undo** (`transfer/history.py`). Whatever a transfer is
    about to overwrite is copied into a local cache first and journalled;
    "Undo replace" (`Ctrl+Z`) and the History window put it back. Pruned by age,
    count and total size.
  - **Comparison by digest** (`transfer/hashing.py`). sha256 for every file with
    folder digests rolled up Merkle-style, so equal folder digests really do mean
    equal contents. Rows are marked `=`, `≠`, `→`, `←` in both panes, and the
    result window uploads or downloads exactly what you tick. On SFTP the whole
    tree is hashed by one remote command instead of thousands of round trips.
  - **Honest folder dates and sizes** (`transfer/treestat.py`). A directory's own
    mtime does not change when a file three levels down does, which made the
    Modified column misleading; folders now report the newest timestamp anywhere
    below them and their real total size.
  - **A deploy-ignore engine** (`transfer/ignore.py`) with full gitignore syntax
    plus a built-in list (`node_modules`, `vendor`, `.git`, caches, `.env`),
    applied to batch transfers, comparisons and the watcher.
  - **A local directory watcher** (`transfer/watcher.py`) that can upload each
    file as your editor saves it. A file is only sent once its size and timestamp
    have settled, so a half-written save never goes up. Polling, deliberately: it
    behaves the same on a local disk, a mapped drive and a NAS share.
  - **Per-pane navigation history** (`transfer/navhistory.py`) - Back, Forward and
    a recent list - plus **mirrored navigation** that keeps both sides on
    matching directories.
  - **Permissions** (`transfer/permissions.py`): presets, a checkbox grid and an
    octal box that stay in step, recursive with a files-only/folders-only scope,
    and a warning before anything world-writable or set-uid.
  - **Symlinks** are shown in italics with their target, are navigable, and can be
    retargeted from the context menu.
  - **Production guard**: on a connection marked production, uploads, deletes,
    symlink changes, commands and watcher syncs all confirm first.
  - **Server-side tools over SSH** (`transfer/remote_exec.py`): archive and unpack
    in place (`tar.gz`, `tar.bz2`, `tar`, `zip`), content search via ripgrep or
    grep, an ncdu-style disk-usage view, `tail -f` log streaming, recursive chmod,
    and one-off commands. Command lines are built with `shlex.quote`, never by
    interpolating input, and there are tests that prove a pattern or path cannot
    turn into a second command.
  - **An embedded SSH shell** (`ui/ssh_terminal_tab.py`) that opens in the
    directory you are looking at, and a **PuTTY / Windows Terminal / ssh.exe /
    WSL launcher** (`transfer/spawn.py`) for when you want the real thing.
  - **A snippet library** (`transfer/snippets.py`): parameterised commands with
    `{remote_dir}`, `{file}` and friends substituted *quoted*.
  - **WinSCP and connection-string import/export** (`transfer/connstr.py`).
    Reads `WinSCP.ini` including its obfuscated passwords, or a file of
    `sftp://user:pass@host:port/path` strings; writes either format back out.
    Session names ending in prod/staging/dev are tinted accordingly on import.
- **Capability flags** (`transfer/base.py`). Backends advertise what they can do
  (`exec`, `chmod`, `symlink`, `set_mtime`, `atomic_replace`) and the UI hides
  what a protocol cannot offer instead of failing at the point of use. SFTP
  probes once at connect time, so SFTP-only hosting accounts that refuse commands
  also get the server-side tools hidden rather than broken. FTP asks the server
  (`FEAT`, `SITE HELP`) about `MFMT` and `SITE CHMOD`.
- The web front end gained the same features: navigation history, sync markers,
  the queue panel, comparison, folder statistics, permissions, and a tools panel
  for search, disk usage, commands, snippets and logs. `backend/` exposes 30 new
  endpoints for them, and its transfer sessions now use the shared pool rather
  than a hand-rolled sequential loop.
- **A second front end: Tauri 2 shell + Svelte 5 UI over the existing Python
  core.** Structured the way RaplMail is - the Rust shell picks a free loopback
  port and a per-launch token, spawns the frozen Python backend as a sidecar,
  and hands both to the webview through one command. Nothing is duplicated: the
  vault, the statement splitter, the result formatter and the FTP/SFTP backends
  are the same modules the PyQt build uses.
  - `backend/` exposes them over HTTP: vault status/unlock/protection, profile
    CRUD with encrypted `.mrx` import/export, native MySQL sessions, and
    FTP/FTPS/SFTP sessions. Transfer progress is pushed over a `/events`
    WebSocket. Every request needs the shared-secret header, and endpoints that
    touch credentials answer 423 while the vault is locked.
  - `backend/app/services/` holds non-Qt session managers - the QObject workers
    reduced to plain threads and a queue.
  - `frontend/` is the UI: icon rail, grouped and searchable connection list,
    the `mysql>` console, the dual-pane transfer view, split panes, and the
    first-run/unlock gate.
- `dev-setup.ps1` and `build-sidecar.ps1`, which make the project buildable from
  the NAS share (see below) and freeze the backend into the sidecar binary.

### Changed
- **The window looks like one application.** Styling used to be scattered - three
  tabs each carrying their own dark-mode CSS, dialogs hard-coding grey hints, and
  no line anywhere between the toolbars and the content, so the window read as a
  single undifferentiated field. There is now one palette and one stylesheet
  (`ui/theme.py`) applied to the whole app, and the file manager is laid out as
  header / work area / footer with real borders between them.
  - The seven shell-only buttons collapsed into one **Server tools** menu (the row
    used to wrap, and on FTP most of it was hidden, leaving a ragged gap), and the
    rarely-used file actions into a **More** menu.
  - Navigation glyphs are painted rather than typed: "◀" and "⟳" are missing from
    some Windows UI fonts and were rendering blank or as a stray letter, and Qt's
    standard icons are a fixed dark grey that vanishes on a dark window.
  - Listings get banded rows, left-aligned headers and a permissions column;
    Fusion's red cross close button on tabs became a quiet ×.
- **Dark mode is the default now, and it is two settings rather than one.**
  "Dark app theme" (`Ctrl+Shift+D`) is the window, tabs, tables and dialogs;
  "Dark phpMyAdmin pages" (`Ctrl+Shift+W`) is the Dark Reader injection into the
  page itself. One switch could not express wanting a dark app around a light
  phpMyAdmin, or the reverse. A settings file written before the split had one
  shared flag defaulting to off, with no way to tell a deliberate "off" from an
  untouched default, so those files are treated as never having chosen and get
  the new dark default; anything saved afterwards sticks.
- **The project is one directory again.** The repo used to live in
  `Z:\MysqlRunner` while `Z:\python\mysql runner` held only the build spec,
  the installer and the frozen exes. Everything was merged into the latter and
  the old directory removed. That split had already caused the 1.0.3 crash - the
  exe was frozen from a tree whose source had gone missing - and left the build
  spec pointing at a `version_info.txt` that existed in only one of the two.
- `.gitignore` now covers the one-file build output, the packaged releases, and
  the front-end dependency and target trees. Roughly 970 MB of artifacts were a
  single `git add` away from being committed.

### Fixed
- **"Import from WinSCP" crashed.** The handler called a helper that was never
  defined, so the menu item was a guaranteed NameError - and PyQt turns an
  unhandled exception in a slot into an aborted process, which is what a crash
  looks like. Two things came out of it:
  - The import now looks where WinSCP actually keeps sessions. An installed
    WinSCP stores them in the registry under HKCU and only uses `WinSCP.ini`
    when told to, so the old code would have found nothing even without the
    crash. Registry first, then the ini locations, then a file picker.
  - There is a test that presses **every** control in the application - 125 menu
    items, buttons, checkboxes, context-menu entries and dialog buttons - and
    reports anything that raises. It would have caught this before shipping;
    nothing else would, because no test had ever pressed that button.
- The installer said 1.0.3 while the exe it wrapped said 1.1.0; both are 1.1.0 now.
- The release virtual environment had only PyQt and cryptography in it, so a build
  from it would have shipped an app whose SQL console and every SFTP feature
  reported a missing driver. The requirements are installed there now, and the
  README says to check it before a release.
- **Deleting a remote folder with anything in it failed.** `rmdir` only removes
  empty directories, so the app reported a bare "directory not empty" and left
  the folder there. Both front ends now walk it, deepest first, and unlink
  symlinked directories instead of following them. Local deletes of a folder
  behave the same way.
- **Cancel pressed while a directory tree was still being walked could be
  ignored.** With the queue built after the walk, a cancel arriving during the
  walk had nothing to stop and the transfers started anyway. The queue is now
  never submitted if a cancel arrived while it was being planned.
- `tar` was invoked as `tar --force-local czf …`, which GNU tar reads as a *file
  named* "czf" - a long option first switches off the old bundled-flags form.
  The dashed form is used now. (`--force-local` itself is there so a path with a
  colon in it is not mistaken for `host:path`.)
- `du` output is no longer matched to the requested path by string equality: a
  trailing slash, a resolved symlink or a relative argument all come back
  changed, which made the total row show up as a child of itself. The total is
  now recognised as the row every other row sits inside.
- **The shared core no longer drags in PyQt6.** `mysql_runner/__init__.py`
  re-exported the Qt entrypoint, so importing anything at all from the package -
  the vault, a model, an FTP client - imported PyQt6. That made the GUI-free
  sidecar impossible to freeze. The re-export is gone; use
  `from mysql_runner.app import run`.
  - A consequence worth knowing: that re-export also imported Qt WebEngine
    early, which Qt requires to happen before any QCoreApplication exists.
    `mysql_runner.app` still does this correctly, but any new GUI entrypoint has
    to do it too.
- The MySQL driver helpers (connect arguments, row cap, error formatting) moved
  from the Qt worker in `db/mysql_client.py` into a new Qt-free
  `db/driver.py`, which both front ends import. `mysql_client` re-exports them,
  so nothing that used it had to change.
- **A stale key in the OS keyring could stop the app starting.** The Data
  Encryption Key is cached in Windows Credential Manager under a fixed service
  name, so the entry is shared by anything on the machine using it - a second
  install, a portable copy, a test run - and is not tied to the vault file it
  came from. Both front ends trusted whatever was cached and then treated the
  resulting decryption failure as fatal: the Qt app reported "Vault error" and
  quit, and the backend raised during FastAPI's lifespan and exited.
  - `storage.store.opens_store()` now answers whether a key actually decrypts
    the store. The Qt flow validates the cached key, discards it if it does not
    fit, and falls through to the password prompt; the backend's auto-unlock is
    non-fatal and simply leaves the vault locked for the UI to handle.
  - `MYSQLRUNNER_NO_KEYRING=1` bypasses the credential store entirely, which
    also keeps test runs from overwriting a real user's cached key.
- **The frozen sidecar hung when launched without stdout/stderr.** Built with
  `console=False`, `sys.stdout` and `sys.stderr` are None unless the parent
  supplies pipes; uvicorn attaches a log handler to them at startup and the
  process then never finished binding its port. Tauri does pipe both streams,
  which is precisely why this stayed hidden until the binary was started any
  other way. The entrypoint now points missing streams at the null device.

### Notes
- The phpMyAdmin browser tab is not ported yet; in the web front end those
  connections open in the system browser for now. Per-tab session isolation is
  reproducible in Tauri (its webview context is keyed by `data_directory`), but
  HTTP Basic Auth is not - wry does not expose WebView2's
  `BasicAuthenticationRequested`.
- Building on the SMB share needs `dev-setup.ps1`: Samba creates files without
  the execute bit, so Windows refuses to launch an `.exe` or load a `.node` from
  it. The durable fix is a `create mask` on the share.

## [1.1.0] - 2026-08-23

### Added
- **Native MySQL console tabs.** A connection can now be a real `mysql>` prompt
  that talks straight to port 3306 instead of driving phpMyAdmin: bordered ASCII
  result tables, `Empty set` / `Query OK` summaries with timings, multi-line
  statements terminated with `;`, `\G` for vertical output, arrow-key history,
  and the `\c` / `\s` / `\r` / `\q` / `\?` backslash commands. The connection
  runs on a worker thread so a slow query never freezes the window, and result
  sets are capped at 5000 rows (with a note) instead of exhausting memory.
- **Dual-pane FTP / FTPS / SFTP file manager.** Transfer connections open a
  WinSCP-style two-pane view - local on the left, remote on the right - with
  recursive folder upload and download, directory creation, rename, delete, and
  a per-file progress bar that can be cancelled mid-queue. FTP and FTPS use the
  standard library (MLSD listings, with a LIST parser as fallback); SFTP uses
  Paramiko and records host keys in `%APPDATA%\Sitekeeper\known_hosts` on a
  trust-on-first-use basis, refusing a later key change for a known host.
  Symlinked directories are not followed, so a self-referential link cannot send
  a recursive transfer into an endless walk.
- **Split view** (`Ctrl+Alt+S`): two tab panes side by side, with `Ctrl+Alt+M` to
  move the current tab across and `Ctrl+Alt+Tab` to switch panes. Turning split
  view back off moves the tabs back rather than closing them.
- **Password protection can be turned off.** Settings now offers a vault
  protection mode. With it off there is no prompt at any point, and the Data
  Encryption Key is sealed with the Windows Data Protection API instead of being
  derived from a master password - so `servers.enc` stays encrypted and is
  useless on another account or machine. Switching either way re-seals the same
  key, leaving stored connections intact. The first-run dialog offers the choice
  too. Idle auto-lock turns itself off in this mode, where re-unlocking would be
  instant anyway.
- Connection profiles gained the fields the new types need (kind, host, port,
  database, remote/local starting directory, private key, passive mode) and the
  Add/Edit dialog now shows only the fields that apply to the chosen type.
  Vaults written by older versions load unchanged - every new field is optional.

### Changed
- **The sidebar collapses instead of just disappearing.** `Ctrl+B` now collapses
  it to a slim rail that keeps an expand button in reach; `Ctrl+Shift+B` still
  hides it completely. Its width is remembered across sessions, and dragging the
  divider almost shut snaps to the rail rather than losing the panel.
- The connection list marks non-phpMyAdmin entries with their protocol and its
  tooltip shows the resolved target (for example `mysql://root@db:3306/shop`).
- Dark mode now also themes the console and file-manager tabs, not just the
  embedded browser.
- Requires `PyMySQL` and `paramiko`; both are named in the PyInstaller spec's
  hidden imports, along with `keyring` (which the vault loads by name, where
  static analysis cannot see it).

### Fixed
- **Add / Edit no longer crashes the app.** `main_window` called `ServerDialog`
  without any module importing it, so both actions raised `NameError` and PyQt6
  aborted the process. This is the same defect the 1.0.3.1 hotfix patched into
  the shipped executable; the source tree still had it, so a fresh build would
  have reproduced the crash.
- Closing a console or transfer tab while it was still connecting could corrupt
  the heap: the worker's reply arrived on a half-deleted widget. Tabs now detach
  from their worker before tearing down, and stop worker threads when the window
  closes.
- `\c` cancels a half-typed statement in the console, as it does in the real
  mysql client, rather than being swallowed into the statement buffer.

## [1.0.3] - 2026-06-23

### Changed
- **Dark mode rewritten.** Dropped the full-page `invert(1) hue-rotate()` CSS
  filter — which only produced a washed-out grey negative and miscoloured
  images — in favour of the bundled [Dark Reader](https://darkreader.org)
  engine. It reads each element's computed colours at runtime and generates
  proper dark equivalents (text, backgrounds, borders, images), watching the
  DOM for changes, so there are no more white-on-white elements or smudged
  fonts. The library is vendored under `mysql_runner/web/vendor/` and bundled
  into the build so dark mode works offline and inside the packaged `.exe`.

### Fixed
- Fixed dark mode failing with `ReferenceError: __PLUS__ is not defined` /
  `DarkReader.enable is not a function`, caused by a broken upstream Dark Reader
  release (4.9.108). Pinned to the clean 4.9.109 build.

## [1.0.2] - earlier

- See the project git history for changes prior to 1.0.3.
