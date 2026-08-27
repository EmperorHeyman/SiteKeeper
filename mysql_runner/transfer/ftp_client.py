"""FTP and FTPS backend built on the standard library's ftplib.

Directory listings prefer MLSD (RFC 3659), which reports type, size and modify
time in a machine-readable form. Servers that predate it fall back to parsing
LIST output, which is best-effort by nature - the Unix-style layout that almost
every server emits is handled, and anything unparseable is shown as a file.

FTP has no shell, so the server-side tools (archives, search, disk usage) are
not offered on these connections. Two optional commands are used when the
server advertises them: ``SITE CHMOD`` for permissions and ``MFMT`` for
timestamps, which is what lets an upload keep the local file's modified date.
"""

from __future__ import annotations

import ftplib
import os
from datetime import datetime, timezone

from mysql_runner.transfer.base import (
    Capability,
    ProgressCallback,
    RemoteEntry,
    RemoteFS,
    RemoteStat,
    TransferError,
)

#: Socket timeout for control-connection commands, in seconds.
TIMEOUT = 20

#: Block size for transfers in both directions. ftplib's default is 8 KB,
#: which costs a socket call per 8 KB and made every transfer feel slow;
#: 128 KB moves the same bytes in a fraction of the calls.
CHUNK = 128 * 1024


class FTPFileSystem(RemoteFS):
    """FTP (optionally over explicit TLS) remote filesystem."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_tls: bool = False,
        passive: bool = True,
    ) -> None:
        self._host = host
        self._port = port or 21
        self._username = username or "anonymous"
        self._password = password
        self._use_tls = use_tls
        self._passive = passive
        self._ftp: ftplib.FTP | None = None
        self._capabilities: frozenset[Capability] = frozenset()

    # ----- lifecycle ------------------------------------------------------
    def connect(self) -> str:
        try:
            ftp: ftplib.FTP = ftplib.FTP_TLS() if self._use_tls else ftplib.FTP()
            ftp.connect(self._host, self._port, timeout=TIMEOUT)
            welcome = ftp.getwelcome() or ""
            ftp.login(self._username, self._password)
            if self._use_tls and isinstance(ftp, ftplib.FTP_TLS):
                # Encrypt the data channel too, not just the control channel.
                ftp.prot_p()
            ftp.set_pasv(self._passive)
            self._ftp = ftp
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc
        self._capabilities = self._detect_capabilities()
        scheme = "FTPS" if self._use_tls else "FTP"
        banner = welcome.strip().splitlines()[0] if welcome.strip() else ""
        return f"{scheme} connected to {self._host}:{self._port}. {banner}".strip()

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except Exception:
            try:
                self._ftp.close()
            except Exception:
                pass
        self._ftp = None

    def _require(self) -> ftplib.FTP:
        if self._ftp is None:
            raise TransferError("Not connected.")
        return self._ftp

    def alive(self) -> bool:
        """NOOP is the protocol's own "are you still there?"."""
        if self._ftp is None:
            return False
        try:
            self._ftp.voidcmd("NOOP")
        except Exception:
            return False
        return True

    # ----- capabilities ---------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def _detect_capabilities(self) -> frozenset[Capability]:
        """Ask the server what optional commands it has.

        Only what the server actually advertises is claimed, so the UI can hide
        what would fail rather than offering it and apologising afterwards.
        """
        found: set[Capability] = set()
        features, feature_text = self._features()
        if "MFMT" in features:
            found.add(Capability.SET_MTIME)
        if self._site_supports("CHMOD", feature_text):
            found.add(Capability.CHMOD)
        return frozenset(found)

    def _features(self) -> tuple[set[str], str]:
        """The FEAT keywords, plus the raw reply (some servers list SITE verbs)."""
        ftp = self._require()
        try:
            response = ftp.sendcmd("FEAT")
        except ftplib.all_errors:
            return set(), ""
        found: set[str] = set()
        for line in response.splitlines()[1:]:
            token = line.strip().split(" ", 1)[0].upper()
            if token and not token.startswith("211"):
                found.add(token)
        return found, response.upper()

    def _site_supports(self, command: str, feature_text: str = "") -> bool:
        """Whether a SITE sub-command is offered.

        ``SITE HELP`` is the reply that lists the sub-commands (``HELP SITE``
        only prints the syntax of SITE itself), but not every server has it, so
        both are tried and FEAT is consulted as well.
        """
        wanted = command.upper()
        if wanted in feature_text:
            return True
        ftp = self._require()
        for probe in ("SITE HELP", "HELP SITE"):
            try:
                response = ftp.sendcmd(probe)
            except ftplib.all_errors:
                continue
            if wanted in response.upper():
                return True
        return False

    # ----- navigation -----------------------------------------------------
    def home(self) -> str:
        ftp = self._require()
        try:
            return ftp.pwd() or "/"
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def listdir(self, path: str) -> list[RemoteEntry]:
        ftp = self._require()
        try:
            ftp.cwd(path or "/")
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

        entries = self._listdir_mlsd(ftp)
        if entries is None:
            entries = self._listdir_line_based(ftp)
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def _listdir_mlsd(self, ftp: ftplib.FTP) -> list[RemoteEntry] | None:
        """Parse MLSD output, or return None when the server lacks it."""
        try:
            listing = list(ftp.mlsd())
        except (ftplib.error_perm, ftplib.error_proto, AttributeError):
            return None
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

        entries: list[RemoteEntry] = []
        for name, facts in listing:
            if name in (".", ".."):
                continue
            kind = facts.get("type", "file")
            if kind in ("cdir", "pdir"):
                continue
            entries.append(
                RemoteEntry(
                    name=name,
                    is_dir=kind == "dir",
                    size=_as_int(facts.get("size")),
                    modified=_parse_mlsd_time(facts.get("modify")),
                    mode=_as_mode(facts.get("unix.mode")),
                )
            )
        return entries

    def _listdir_line_based(self, ftp: ftplib.FTP) -> list[RemoteEntry]:
        """Fall back to parsing LIST output line by line."""
        lines: list[str] = []
        try:
            ftp.retrlines("LIST", lines.append)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc
        entries: list[RemoteEntry] = []
        for line in lines:
            entry = _parse_list_line(line)
            if entry is not None and entry.name not in (".", ".."):
                entries.append(entry)
        return entries

    def stat(self, path: str) -> RemoteStat:
        """Use SIZE and MDTM when the server has them; else read the parent."""
        size = self._size(path)
        modified = self._modified(path)
        if size or modified is not None:
            return RemoteStat(path=path, is_dir=False, size=size, modified=modified)
        return super().stat(path)

    def _modified(self, path: str) -> float | None:
        ftp = self._require()
        try:
            response = ftp.sendcmd(f"MDTM {path}")
        except ftplib.all_errors:
            return None
        parts = response.strip().split()
        if len(parts) < 2:
            return None
        return _parse_mlsd_time(parts[1])

    # ----- mutations ------------------------------------------------------
    def mkdir(self, path: str) -> None:
        try:
            self._require().mkd(path)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def remove(self, path: str) -> None:
        try:
            self._require().delete(path)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def rmdir(self, path: str) -> None:
        try:
            self._require().rmd(path)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def rename(self, source: str, target: str) -> None:
        try:
            self._require().rename(source, target)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def chmod(self, path: str, mode: int) -> None:
        self._require_capability(Capability.CHMOD)
        octal = format(mode & 0o7777, "o")
        try:
            self._require().sendcmd(f"SITE CHMOD {octal} {path}")
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def set_mtime(self, path: str, mtime: float) -> None:
        self._require_capability(Capability.SET_MTIME)
        stamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        try:
            self._require().sendcmd(f"MFMT {stamp} {path}")
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    # ----- transfers ------------------------------------------------------
    def download(
        self, remote: str, local: str, progress: ProgressCallback | None = None
    ) -> None:
        ftp = self._require()
        total = self._size(remote)
        transferred = 0
        try:
            with open(local, "wb") as handle:

                def write(chunk: bytes) -> None:
                    nonlocal transferred
                    handle.write(chunk)
                    transferred += len(chunk)
                    if progress is not None:
                        progress(transferred, total)

                ftp.retrbinary(f"RETR {remote}", write, blocksize=CHUNK)
        except ftplib.all_errors as exc:
            # Leave no half-written file behind.
            try:
                os.unlink(local)
            except OSError:
                pass
            raise TransferError(_describe(exc)) from exc

    def upload(
        self, local: str, remote: str, progress: ProgressCallback | None = None
    ) -> None:
        ftp = self._require()
        try:
            total = os.path.getsize(local)
        except OSError as exc:
            raise TransferError(_describe(exc)) from exc
        transferred = 0

        def sent(chunk: bytes) -> None:
            nonlocal transferred
            transferred += len(chunk)
            if progress is not None:
                progress(transferred, total)

        try:
            with open(local, "rb") as handle:
                ftp.storbinary(f"STOR {remote}", handle, blocksize=CHUNK, callback=sent)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc

    def stream_download(self, remote: str, sink, progress=None) -> int:
        """Read a remote file straight through - used for hashing."""
        ftp = self._require()
        total = self._size(remote)
        transferred = 0

        def receive(chunk: bytes) -> None:
            nonlocal transferred
            transferred += len(chunk)
            sink(chunk)
            if progress is not None:
                progress(transferred, total)

        try:
            ftp.retrbinary(f"RETR {remote}", receive, blocksize=CHUNK)
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc
        return transferred

    def _size(self, remote: str) -> int:
        """Best-effort file size; 0 when the server will not say."""
        ftp = self._require()
        try:
            ftp.voidcmd("TYPE I")  # SIZE is only valid in binary mode.
            return ftp.size(remote) or 0
        except Exception:
            return 0


# ----- parsing helpers ----------------------------------------------------
def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_mode(value: object) -> int | None:
    """Parse an MLSD ``unix.mode`` fact ("0644") into permission bits."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(value.strip(), 8) & 0o7777
    except ValueError:
        return None


def _parse_mlsd_time(value: object) -> float | None:
    """Parse an MLSD "modify" fact (YYYYMMDDHHMMSS, UTC)."""
    if not isinstance(value, str) or len(value) < 14:
        return None
    try:
        stamp = datetime.strptime(value[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc).timestamp()


def _mode_from_permissions(text: str) -> int | None:
    """Turn "rwxr-xr-x" from a LIST line into permission bits."""
    body = text[1:10]
    if len(body) != 9:
        return None
    mode = 0
    for index, char in enumerate(body):
        if char == "-":
            continue
        bit = (0b100, 0b010, 0b001)[index % 3]
        mode |= bit << (6 - 3 * (index // 3))
    return mode


def _parse_list_line(line: str) -> RemoteEntry | None:
    """Parse one Unix-style LIST line, e.g.

    ``drwxr-xr-x 2 user group 4096 Jan 14 09:31 uploads``

    Returns None for lines that are not entries (totals, blanks).
    """
    parts = line.split(maxsplit=8)
    if len(parts) < 9 or not parts[0]:
        return None
    permissions = parts[0]
    if permissions[0] not in "-dl":
        return None
    name = parts[8]
    is_link = permissions[0] == "l"
    target = ""
    if is_link and " -> " in name:
        name, target = name.split(" -> ", 1)
    return RemoteEntry(
        name=name,
        is_dir=permissions[0] == "d",
        size=_as_int(parts[4]),
        modified=None,  # LIST timestamps are ambiguous; skip rather than guess.
        is_link=is_link,
        mode=_mode_from_permissions(permissions),
        link_target=target,
    )


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
