# FTP/SFTP feature plan

**Status: all 23 features implemented, wired into both front ends, and covered
by tests (627 checks across 17 suites).**

Every idea in `ideas_for_ftp.md`, mapped to where it lives. Core modules are
Qt-free so both front ends (and the tests) can use them; the Qt column is where
the feature surfaces in the desktop app.

## Core layer (`mysql_runner/transfer/`)

| Module | Purpose |
| --- | --- |
| `base.py` | `RemoteFS` + capability flags (`exec`, `chmod`, `symlink`, `set_mtime`, `atomic_replace`), `RemoteStat`, streaming reads |
| `navhistory.py` | Back / forward / up stack per pane, with a bounded memory |
| `ignore.py` | `.deployignore` / `.gitignore` parser (negation, anchors, `**`, dir-only) |
| `hashing.py` | Local + remote file and folder digests, and the two-sided comparison |
| `treestat.py` | Recursive folder size / newest-content mtime, exec fast path |
| `connstr.py` | Connection-string and WinSCP.ini import + export |
| `history.py` | Shadow backups of overwritten files, and the undo journal |
| `pool.py` | Multi-threaded transfer engine: priorities, pause/resume, atomic uploads |
| `remote_exec.py` | Archive/extract, grep, disk usage, tail, one-off commands |
| `permissions.py` | Octal helpers and chmod presets |
| `snippets.py` | Saved snippet library (persisted JSON) |
| `watcher.py` | Polling local directory watcher for background sync |
| `syncrules.py` | Synced folders: local/remote pairs, their trigger, persistence |
| `gitwatch.py` | Commit detection by reading `HEAD`, refs and the reflog |
| `spawn.py` | External terminal (PuTTY / Windows Terminal / kitty) command builder |

## The 23 features

| # | Feature | Core | UI | Tested by |
| --- | --- | --- | --- | --- |
| ✅ 1 | Back button with memory, un-replace | `navhistory`, `history` | pane toolbar, History dialog | test_ftp_core, test_ftp_gui |
| ✅ 2 | WinSCP import/export, connection strings | `connstr` | File menu | test_ftp_core |
| ✅ 3 | Folder + file hashes, show differences | `hashing` | Compare button, row tinting | test_ftp_core, test_ftp_exec, test_ftp_gui |
| ✅ 4 | Correct folder modified date | `treestat` | Modified column | test_ftp_core, test_ftp_exec, test_ftp_gui |
| ✅ 5 | Multi-threaded transfers | `pool` | Settings: concurrency | test_ftp_core, test_ftp_gui |
| ✅ 6 | Server-side archive / unpack | `remote_exec` | remote context menu | test_ftp_exec, test_ftp_shell_gui |
| ✅ 7 | Atomic uploads | `pool`, `base.replace` | Settings: safe deploy | test_ftp_core, test_ftp_gui |
| ✅ 8 | Shadow backups | `history` | automatic; History dialog | test_ftp_core, test_ftp_gui |
| ✅ 9 | Deploy-ignore engine | `ignore` | Settings + Compare/sync | test_ftp_core, test_ftp_gui |
| ✅ 10 | Background directory watcher | `watcher` | Watch toggle in the tab | test_ftp_core, test_ftp_gui |
| ✅ 11 | Separate navigation channel | `pool` (nav connection reserved) | transparent | test_ftp_gui |
| ✅ 12 | Queue priority + pause | `pool` | Transfer queue panel | test_ftp_core, test_ftp_gui |
| ✅ 13 | Production guard | `permissions`-independent | red banner, confirm modals | test_ftp_gui |
| ✅ 14 | Context terminal | `remote_exec` (interactive shell) | SSH terminal tab | test_ftp_shell_gui |
| ✅ 15 | Live log streaming (`tail -f`) | `remote_exec.tail` | Log viewer | test_ftp_exec, test_ftp_shell_gui |
| ✅ 16 | External shell spawner | `spawn` | toolbar button | test_ftp_core |
| ✅ 17 | Remote command quick-runner | `remote_exec.run` | command bar (Ctrl+P) | test_ftp_shell_gui |
| ✅ 18 | Snippet library | `snippets` | snippet drawer | test_ftp_core, test_ftp_shell_gui |
| ✅ 19 | Remote grep | `remote_exec.grep` | Search dialog | test_ftp_exec, test_ftp_shell_gui |
| ✅ 20 | Disk usage heatmap | `remote_exec.disk_usage` | Disk usage dialog | test_ftp_exec, test_ftp_shell_gui |
| ✅ 21 | Symlink badges + retarget | `base.readlink/symlink` | badge, Link target dialog | test_ftp_e2e, test_ftp_gui |
| ✅ 22 | Mirror navigation | `navhistory.mirror_path` | Mirror toggle | test_ftp_core, test_ftp_gui |
| ✅ 23 | chmod presets | `permissions` | Permissions dialog | test_ftp_core, test_ftp_gui, test_ftp_exec |

Features 6, 14-20 need a shell, so they are offered only for SFTP; the file
manager hides them for FTP/FTPS rather than failing at the point of use.
