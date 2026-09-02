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

from mysql_runner.transfer import longlist
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
        #: Set from FEAT at login. MLST answers "what is this path" in one
        #: round trip and says whether it is a directory; REST is what lets an
        #: interrupted transfer carry on instead of starting again.
        self._has_mlst = False
        self._has_rest = False

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
        # Neither of these is a Capability - nothing in the UI turns on them -
        # but both decide how many round trips ordinary work costs.
        self._has_mlst = "MLST" in features
        self._has_rest = "REST" in features
        return frozenset(found)

    def supports_resume(self) -> bool:
        """Whether this server advertised REST, the restart-marker command."""
        return self._has_rest

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
                    owner=_fact(facts, "owner"),
                    group=_fact(facts, "group"),
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
            entry = longlist.parse_line(line)
            if entry is not None and entry.name not in (".", ".."):
                entries.append(entry)
        return entries

    def stat(self, path: str) -> RemoteStat:
        """What one path is, in as few round trips as the server allows.

        This used to ask SIZE and then MDTM, and report ``is_dir`` as False if
        either of them answered - so a server willing to give a directory a
        size described that directory as a file, and a directory that refused
        both paid for two useless commands *before* falling back to reading the
        parent listing anyway. Five round trips for a folder, three for a file,
        and a wrong answer on some servers.

        Both replacements are authoritative about type, which is the thing SIZE
        can never be. MLST is one command and says outright what the path is.
        Without it the parent listing (CWD + MLSD/LIST, two commands) settles
        it, and that is still fewer round trips than the pair of guesses it
        replaces - on a link where the round trips are the cost, which is the
        only kind this app is used over.
        """
        if self._has_mlst:
            found = self._mlst(path)
            if found is not None:
                return found
        return super().stat(path)

    def _mlst(self, path: str) -> RemoteStat | None:
        """One MLST command. None when the server would not answer it."""
        ftp = self._require()
        try:
            response = ftp.sendcmd(f"MLST {path}")
        except ftplib.all_errors:
            return None
        # 250-Listing /x \r\n <facts> /x \r\n 250 End. The facts are on the
        # indented middle line; the path follows the first space after them.
        for line in response.splitlines():
            if not line.startswith(" "):
                continue
            facts_text = line.strip().split(" ", 1)[0]
            facts: dict[str, str] = {}
            for part in facts_text.split(";"):
                if "=" in part:
                    key, _, value = part.partition("=")
                    facts[key.strip().lower()] = value.strip()
            if not facts:
                continue
            kind = facts.get("type", "file").lower()
            is_dir = kind in ("dir", "cdir", "pdir")
            return RemoteStat(
                path=path,
                is_dir=is_dir,
                size=0 if is_dir else _as_int(facts.get("size")),
                modified=_parse_mlsd_time(facts.get("modify")),
                mode=_as_mode(facts.get("unix.mode")),
                owner=_fact(facts, "owner"),
                group=_fact(facts, "group"),
            )
        return None

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
        self,
        remote: str,
        local: str,
        progress: ProgressCallback | None = None,
        *,
        resume_from: int = 0,
        keep_partial: bool = False,
    ) -> None:
        ftp = self._require()
        total = self._size(remote)
        start = resume_from if resume_from > 0 and self._has_rest else 0
        transferred = start
        try:
            # "r+b" seeks into what is already there; "wb" starts a fresh file.
            # Truncating at the mark matters as much as seeking to it: the tail
            # of an interrupted download is whatever arrived of the last block,
            # and REST counts from the mark, not from that.
            with open(local, "r+b" if start else "wb") as handle:
                if start:
                    handle.seek(start)
                    handle.truncate(start)

                def write(chunk: bytes) -> None:
                    nonlocal transferred
                    handle.write(chunk)
                    transferred += len(chunk)
                    if progress is not None:
                        progress(transferred, total)

                ftp.retrbinary(
                    f"RETR {remote}", write, blocksize=CHUNK, rest=start or None
                )
        except ftplib.all_errors as exc:
            if not keep_partial:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            raise TransferError(_describe(exc)) from exc

    def upload(
        self,
        local: str,
        remote: str,
        progress: ProgressCallback | None = None,
        *,
        resume_from: int = 0,
    ) -> None:
        ftp = self._require()
        try:
            total = os.path.getsize(local)
        except OSError as exc:
            raise TransferError(_describe(exc)) from exc
        start = resume_from if resume_from > 0 and self._has_rest else 0
        if start >= total:
            return  # everything is already up there
        transferred = start

        def sent(chunk: bytes) -> None:
            nonlocal transferred
            transferred += len(chunk)
            if progress is not None:
                progress(transferred, total)

        try:
            with open(local, "rb") as handle:
                handle.seek(start)
                ftp.storbinary(
                    f"STOR {remote}",
                    handle,
                    blocksize=CHUNK,
                    callback=sent,
                    rest=start or None,
                )
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

    def read_range(self, remote: str, offset: int = 0, length: int = 0) -> bytes:
        """Restart the transfer at ``offset`` and keep ``length`` bytes.

        REST is the resume command, and reading the end of a log is the same
        request as resuming a download that stopped there. Servers that never
        advertised REST get the base class's read-and-slice instead, which is
        correct and slow rather than wrong.

        The bytes past the window are read and dropped rather than the
        transfer being cut short: aborting an FTP data connection mid-stream
        leaves the control channel in a state the next command has to clean
        up, and a tail is offset-to-end anyway, so there is usually nothing
        past the window to drop.
        """
        offset = max(0, int(offset))
        if not self._has_rest:
            return super().read_range(remote, offset, length)
        ftp = self._require()
        buffer = bytearray()
        wanted = length or 0

        def receive(chunk: bytes) -> None:
            if wanted and len(buffer) >= wanted:
                return
            buffer.extend(chunk)

        try:
            ftp.voidcmd("TYPE I")
            ftp.retrbinary(
                f"RETR {remote}", receive, blocksize=CHUNK, rest=offset or None
            )
        except ftplib.all_errors as exc:
            raise TransferError(_describe(exc)) from exc
        return bytes(buffer[:wanted]) if wanted else bytes(buffer)

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


def _fact(facts: dict, which: str) -> str:
    """An MLSD ownership fact, by whichever name this server uses for it.

    Ownership is not in RFC 3659, so servers that report it at all disagree
    about the spelling: ProFTPD sends ``unix.owner`` (a name), others send
    ``unix.ownername`` beside a numeric ``unix.owner``. A name is worth more
    than a number here, so it wins when both are there.
    """
    for key in (f"unix.{which}name", f"unix.{which}", which):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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


def _describe(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
