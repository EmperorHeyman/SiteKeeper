All of these are now implemented. `[x]` marks the ones that are done, with a
note saying where each one lives; the original wording is untouched. See
`ideas_for_ftp_plan.md` for the module map.

[x] 1: go back btn with memory... when I replace files there should be a btn to un-replace them
    → Back / Forward / recent-list per pane (`transfer/navhistory.py`), and
      "Undo replace" plus the History window, backed by shadow backups
      (`transfer/history.py`, `ui/history_dialog.py`). Ctrl+Z undoes the last one.

[x] 2:import + export connections from winSCP with connection strings (url like: sftp://a123systems-hr:some_password@webhosting.cloudcore.cz:2202/)
    → File ▸ "Import from WinSCP or a URL list…", "Add from a connection string…",
      "Export for WinSCP…" (`transfer/connstr.py`). WinSCP's password scramble is
      decoded on import and written back on export.

[x] 3:calculated heshes of folders + files to show diferent files on server and in local
    → "Compare" (F9): sha256 for every file, folder digests rolled up from their
      contents, verdict shown in a Sync column in both panes and in a window you
      can upload or download straight from (`transfer/hashing.py`,
      `ui/compare_dialog.py`). Server-side hashing when the host has sha256sum.

[x] 4:actually show correct date changed on a folder when its contains change
    → Folders now report the newest timestamp anywhere below them, and their real
      total size (`transfer/treestat.py`). One find(1) call where possible.

[x] 5:multi-threaded connection -- send/receive more files at once
    → A pool of separate connections, count set in Settings ▸ File transfer
      (`transfer/pool.py`).

[x] Server-side Archiving & Unpacking: Create or extract .zip and .tar.gz archives directly on the server over SFTP/SSH to bypass downloading and uploading thousands of small files.
    → Right-click ▸ "Archive on the server…" / "Unpack here…"
      (`transfer/remote_exec.py`). tar.gz, tar.bz2, tar and zip.

[x] Atomic Uploads (Safe Deploy): Upload to a temporary file (filename.ext.tmp_uuid) and rename it atomically upon verification to eliminate zero-byte downtime or half-written scripts during live web requests.
    → On by default. Uses OpenSSH's posix-rename where the server has it, so the
      swap really is atomic; falls back to unlink-then-rename elsewhere.

[x] Shadow Backups for Instant Undo: Automatically preserve the previous version of a file in a hidden cache (.history/ or remote tmp) before overwriting, providing a reliable backend for the un-replace action.
    → %APPDATA%\MySQLRunner\history, pruned by age, count and total size.

[x] Deploy Ignore Engine: Native .deployignore / .gitignore parser to skip vendor/, node_modules/, .git/, .env, and cache folders during batch syncs and hash comparisons.
    → `transfer/ignore.py`, full gitignore syntax, plus a built-in default list.
      Applies to batch transfers, comparisons and the watcher.

[x] Background Directory Watcher: Automatic background sync that monitors local directory changes and uploads modified files within milliseconds of saving in an external editor.
    → The "Watch" toggle. A file is uploaded once its size and timestamp settle,
      so a half-written save never goes up (`transfer/watcher.py`).

[x] Separated UI & Transfer Worker Pools: Keep one dedicated SFTP connection channel exclusively for directory navigation so browsing never freezes while heavy file transfers run in the background.
    → Three channels: navigation, the transfer pool, and a third for slow
      read-only jobs (comparisons, folder sizes, searches).

[x] Transfer Queue Prioritization & Pause: Full control over active transfers with pause/resume support and drag-and-drop priority ordering.
    → The Queue panel: pause and resume mid-file, cancel one item or all, drag
      rows to reorder, "Transfer next" to jump the queue.

[x] Environment Safeguards (Production Guard): Color-coded tabs and warning barriers for production servers (e.g., red accent, explicit confirmation modals for destructive deletes or batch replaces).
    → Red tab and banner as before, plus a confirmation before uploads, deletes,
      symlink changes, commands and watcher syncs on a production connection.

[x] One-Click Context Terminal: Open an embedded SSH terminal directly into the active working directory of the current SFTP tab.
    → "Terminal" (Ctrl+T) opens a shell tab already cd'd into the remote
      directory you are looking at (`ui/ssh_terminal_tab.py`).

[x] Live Remote Log Streaming: Built-in tail -f log viewer to stream server error and access logs without downloading multi-gigabyte log files.
    → "Logs" finds the log files near you and follows one, with a filter, pause
      and save (`ui/log_viewer.py`).

[x] Instant PuTTY / Native Shell Spawner: A dedicated hotkey/button that launches your preferred external terminal (PuTTY, Windows Terminal, Kitty) passing host, port, key/password, and auto-running cd /current/remote/path.
    → The "PuTTY" button. Finds PuTTY, KiTTY, Windows Terminal, ssh.exe or WSL;
      which one it prefers, and whether it passes the password, is in Settings
      (`transfer/spawn.py`).

[x] Remote Command Quick-Runner: A slim command bar (like Ctrl+P in VS Code) to run non-interactive one-off commands (e.g., systemctl restart nginx, composer install, git pull, php artisan cache:clear) directly on the server without opening a full terminal session.
    → "Command…" (Ctrl+P), with its own history and the output underneath.

[x] Saved Snippet Library: A sidebar drawer for parameterized bash snippets and deployment scripts you can execute against the active server with one click.
    → "Snippets": nine shipped to start with, editable, with {remote_dir},
      {file} and friends substituted *quoted* (`transfer/snippets.py`).

[x] Remote ripgrep / Grep Search: Search for string patterns or regex across entire remote codebases server-side instead of scanning files one-by-one over SFTP.
    → "Search…" (Ctrl+Shift+F): ripgrep when the server has it, grep otherwise;
      double-click a hit to jump to its folder.

[x] Remote Disk Usage Heatmap (ncdu style): Visual disk usage analyzer to instantly spot which folder or log file is eating server disk space without manual du -sh * commands.
    → "Disk usage": one du level at a time, biggest first, with share bars, and
      you can walk into a folder or jump to it in the file list.

[x] Smart Symlink Management & Indicator: Clear visual badging for symlinks with an option to follow, edit target paths directly in the UI, or toggle between physical and release directories.
    → Links are shown in italics as `name → target`, are navigable, and
      right-click ▸ "Link target…" retargets one (which is how you switch which
      release is live).

[x] Multi-Tab Path Sync (Mirror Navigation): A toggle to lock local and remote directory trees so navigating down a folder locally automatically navigates to the matching folder on the remote server.
    → The "Mirror" toggle, in both directions (`navhistory.mirror_path`).

[x] Quick Permission Presets (Octal Calculator): Visual chmod switcher with one-click presets (755/644 for web roots, 400 for keys, 775 for shared groups) and an option to apply recursively to files only vs folders only.
    → Right-click ▸ "Permissions…": presets, the nine checkboxes and the octal
      box all in step, recursive with a files-only / folders-only scope, and a
      warning on world-writable or set-uid modes (`transfer/permissions.py`).

---

Notes, since they change what to expect:

* Everything that needs a shell (archives, search, disk usage, terminal, logs,
  commands, snippets, recursive chmod) is SFTP-only, and is hidden rather than
  broken on FTP/FTPS. The app probes once at connect time, so SFTP-only hosting
  accounts that refuse commands also get those buttons hidden.
* A negation in an ignore file cannot re-include a file whose parent directory is
  excluded. That is git's own rule, and this follows it.
* WinSCP passwords are obfuscated, not encrypted. Importing brings them into
  this app's encrypted vault; exporting with passwords writes them back out in
  that weak form, so the export asks first.
