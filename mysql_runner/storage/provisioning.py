"""Claim tickets: taking connections from a hosting provider, safely.

A customer clicks *Open in Sitekeeper* in their hosting control panel. What
reaches this machine is not a credential - it is a **claim ticket**::

    sitekeeper://provision/v1?src=panel.example.com&t=<ticket>

or the same two values in a ``.skc`` file, for browsers that have got stricter
about custom schemes. Sitekeeper then fetches the actual connections from the
provider over HTTPS, presenting the ticket once. Credentials travel one way
only: over TLS, in a response, to a request the user just agreed to.

The whole reason for the indirection is that a URL handed to a shell handler is
not private. It lands in browser history, in the address bar, in the panel's
access log, in the referrer header, in the clipboard the moment anyone copies
the link, and on Windows in a process command line that other processes can
read. A ticket in all those places is worthless ten minutes later; a password
in all those places is a password in all those places.

What this module will not do is as much of the design as what it will:

* only HTTPS, only a chain the system trusts, and only a *name* - an ``src``
  that is an IP literal, or that resolves onto the loopback or a private range,
  is refused, so a link cannot aim the app at the user's own network,
* at most two redirects, and never off the host the user was shown,
* a fixed path, derived from ``src``. There is deliberately no ``url=``
  parameter: a caller-supplied URL in a link anyone can send is the hole this
  design exists to close,
* a strict allowlist over the payload. Three of the fields a profile can carry
  are ways to run something - see :data:`REFUSED_FIELDS` - and none of them are
  accepted from a stranger, silently or otherwise.

Nothing here asks the user anything. The confirmation is the caller's job
(``ui/provision_dialog.py``), and it is not optional.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from mysql_runner.paths import provisioned_key_path
from mysql_runner.storage.models import (
    AuthType,
    ConnectionKind,
    Environment,
    ServerProfile,
)

#: The scheme registered with Windows by the installer.
SCHEME = "sitekeeper"
#: The contract version. A future v2 is a new path; v1 keeps working.
CONTRACT_VERSION = 1
#: Path on the provider's host. Fixed, not caller-supplied - see module docs.
ENDPOINT_PATH = "/.well-known/sitekeeper/v1/provision"
#: Extension of the offline form of the same ticket.
CLAIM_SUFFIX = ".skc"

#: Seconds to wait for the provider. Long enough for a panel that has to mint a
#: sub-account on the fly, short enough that a dead host is not a hang.
TIMEOUT = 15.0
#: Response body cap. The payload is a handful of connections; anything larger
#: is either a mistake or someone using the app as a download client.
MAX_BYTES = 256 * 1024
#: Connections accepted from one ticket.
MAX_CONNECTIONS = 50
#: Redirect hops allowed, same host only.
MAX_REDIRECTS = 2
#: Longest ticket accepted, in characters.
MAX_TICKET = 512

#: Profile fields that are never taken from a payload, and why. These are
#: dropped quietly rather than failing the whole import - a provider sending
#: one has made a mistake, and punishing the customer for it helps nobody -
#: but they are reported back so the provider can be told.
REFUSED_FIELDS = {
    "proxy_command": "runs a local command on connect",
    "local_dir": "picks a folder on this PC",
    "startup_script": "runs SQL automatically on connect",
    "private_key_path": "points into this PC's filesystem",
    "jump_profile_id": "means nothing outside the vault that wrote it",
    "id": "could overwrite an existing connection",
    "order": "is decided by this machine",
}

#: Connection kinds a provider may ask for, by their wire name.
_KINDS = {
    "sftp": ConnectionKind.SFTP,
    "ftp": ConnectionKind.FTP,
    "ftps": ConnectionKind.FTPS,
    "mysql": ConnectionKind.MYSQL,
    "mssql": ConnectionKind.MSSQL,
    "phpmyadmin": ConnectionKind.PHPMYADMIN,
}

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                      r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$")

#: Characters stripped from anything a provider supplies for display. The
#: bidirectional overrides are the interesting ones: without them a label can
#: reorder the sentence it is quoted in, so a dialog asking "do you trust
#: this?" would let the thing being trusted rewrite the question.
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩‎‏"


class ProvisioningError(Exception):
    """Raised when a claim cannot be read, fetched, or trusted."""


def clean_text(value: object, *, limit: int = 120) -> str:
    """Make provider-supplied text safe to put in a sentence.

    Control characters and bidi overrides go, whitespace collapses, and the
    result is cut to ``limit``. Everything shown to the user goes through here.
    """
    text = str(value or "")
    text = "".join(ch for ch in text if ch not in _BIDI)
    text = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch
        for ch in text
    )
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


@dataclass
class Claim:
    """A ticket and the host it will be presented to. No credentials here."""

    host: str
    port: int
    ticket: str

    @property
    def source(self) -> str:
        """What the user is shown before anything leaves the machine."""
        return self.host if self.port == 443 else f"{self.host}:{self.port}"

    @property
    def endpoint(self) -> str:
        return f"https://{self.source}{ENDPOINT_PATH}"


@dataclass
class Offer:
    """What a provider answered with, once it has been checked over."""

    source: str
    issuer: str
    profiles: list[ServerProfile] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)  # profile id -> PEM
    expires_at: str = ""
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# ----- reading a claim ----------------------------------------------------
def looks_like_claim(argument: str) -> bool:
    """Whether ``argument`` is something this module should be handed."""
    text = (argument or "").strip().strip('"')
    if text.lower().startswith(f"{SCHEME}:"):
        return True
    return text.lower().endswith(CLAIM_SUFFIX)


def parse_link(text: str) -> Claim:
    """Read ``sitekeeper://provision/v1?src=…&t=…``."""
    raw = (text or "").strip().strip('"')
    if not raw:
        raise ProvisioningError("Empty link.")
    parts = urlsplit(raw)
    if parts.scheme.lower() != SCHEME:
        raise ProvisioningError(
            f"That is not a {SCHEME}:// link."
        )
    # netloc is "provision" and path is "/v1"; some shells hand over
    # "sitekeeper:provision/v1?..." with no slashes, so accept both shapes.
    route = f"{parts.netloc}{parts.path}".strip("/").lower()
    if not route.startswith("provision"):
        raise ProvisioningError("This link is not a connection handover.")
    version = route.rsplit("/", 1)[-1]
    if version not in ("provision", f"v{CONTRACT_VERSION}"):
        raise ProvisioningError(
            f"This link needs a newer Sitekeeper (it asks for {clean_text(version, limit=12)})."
        )
    query = parse_qs(parts.query, keep_blank_values=False)
    src = (query.get("src") or [""])[0]
    ticket = (query.get("t") or [""])[0]
    return build_claim(src, ticket)


def parse_claim_file(path: str | Path) -> Claim:
    """Read a ``.skc`` file: the same two values, as JSON."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProvisioningError("That file could not be read.") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ProvisioningError("That file is not a Sitekeeper claim.") from exc
    if not isinstance(data, dict):
        raise ProvisioningError("That file is not a Sitekeeper claim.")
    version = data.get("version", CONTRACT_VERSION)
    if str(version) != str(CONTRACT_VERSION):
        raise ProvisioningError(
            "This claim file needs a newer Sitekeeper."
        )
    return build_claim(str(data.get("src", "")), str(data.get("ticket", "")))


def load_claim(argument: str) -> Claim:
    """Read whichever form of claim ``argument`` is."""
    text = (argument or "").strip().strip('"')
    if text.lower().startswith(f"{SCHEME}:"):
        return parse_link(text)
    return parse_claim_file(text)


def build_claim(src: str, ticket: str) -> Claim:
    """Validate the two values a claim carries, and pair them up."""
    ticket = unquote(ticket or "").strip()
    if not ticket:
        raise ProvisioningError("This link carries no ticket.")
    if len(ticket) > MAX_TICKET:
        raise ProvisioningError("This link's ticket is not a valid one.")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) > 0x7E for ch in ticket):
        raise ProvisioningError("This link's ticket is not a valid one.")
    host, port = _split_source(unquote(src or "").strip())
    return Claim(host=host, port=port, ticket=ticket)


def _split_source(src: str) -> tuple[str, int]:
    """Turn ``src`` into a host and a port, refusing anything else.

    ``src`` is a name and optionally a port. Not a URL: no scheme, no path, no
    ``user@``. Each of those would be a way to make the host the user is shown
    differ from the host actually dialled.
    """
    if not src:
        raise ProvisioningError("This link does not say where to fetch from.")
    # The bracketed form and bare IPv6 both carry extra colons, which would
    # otherwise be read as a nonsense port and refused with a misleading
    # reason. They are refused here, for the reason that is actually true.
    if (
        "://" in src
        or "/" in src
        or "@" in src
        or "?" in src
        or "#" in src
        or "[" in src
        or src.count(":") > 1
    ):
        raise ProvisioningError(
            "This link's source is not a plain hostname, so it cannot be trusted."
        )
    host, _, port_text = src.partition(":")
    host = host.strip().rstrip(".").lower()
    port = 443
    if port_text:
        if not port_text.isdigit():
            raise ProvisioningError("This link's port is not a number.")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ProvisioningError("This link's port is out of range.")
    if not host:
        raise ProvisioningError("This link does not say where to fetch from.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A certificate can be issued for an IP, but a bare address in a link is
        # the shape every "connect to my box" attack takes and it tells the user
        # nothing they can check. Names only.
        raise ProvisioningError(
            "This link points at an IP address rather than a hostname."
        )
    if not _HOST_RE.match(host):
        raise ProvisioningError("This link's hostname is not a valid one.")
    return host, port


# ----- fetching -----------------------------------------------------------
def fetch(claim: Claim) -> Offer:
    """Present the ticket and return what the provider offered.

    Blocking, and meant to be called off the GUI thread.
    """
    status, body = _request(claim.host, claim.port, ENDPOINT_PATH, claim.ticket)
    if status != 200:
        raise ProvisioningError(_explain(status, body))
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProvisioningError(
            f"{claim.source} answered with something that is not a connection offer."
        ) from exc
    return read_offer(data, source=claim.source)


def _addresses(host: str, port: int) -> None:
    """Refuse a name that resolves somewhere it has no business pointing.

    This is checked before connecting rather than after: by the time a socket is
    open to 127.0.0.1 the request has already been made. A name can of course
    resolve differently a millisecond later - there is no fixing that from here
    without owning the resolver - but the case this closes is the deliberate
    one, a link naming a host whose A record is the user's own router.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProvisioningError(f"{host} could not be looked up.") from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ProvisioningError(
                f"{host} resolves to an address on a private network, so this "
                "link is not a hosting provider's."
            )


def _request(
    host: str, port: int, path: str, ticket: str, hops: int = 0
) -> tuple[int, bytes]:
    """One HTTPS GET, with the ticket, following same-host redirects only."""
    _addresses(host, port)
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        connection = http.client.HTTPSConnection(
            host, port, timeout=TIMEOUT, context=context
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Authorization": f"Bearer {ticket}",
                    "Accept": "application/json",
                    "User-Agent": _user_agent(),
                    "Cache-Control": "no-store",
                    # The ticket is the secret; nothing about it should be
                    # kept alive on a pooled connection after this.
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            status = response.status
            location = response.getheader("Location", "") or ""
            body = response.read(MAX_BYTES + 1)
        finally:
            connection.close()
    except ssl.SSLCertVerificationError as exc:
        raise ProvisioningError(
            f"{host} could not prove it is {host}. Its certificate did not "
            "check out, so nothing was sent."
        ) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ProvisioningError(f"{host} could not be reached.") from exc

    if status in (301, 302, 303, 307, 308):
        if hops >= MAX_REDIRECTS:
            raise ProvisioningError(f"{host} redirected too many times.")
        next_host, next_port, next_path = _follow(host, port, path, location)
        return _request(next_host, next_port, next_path, ticket, hops + 1)
    if len(body) > MAX_BYTES:
        raise ProvisioningError(f"{host} answered with far more data than a "
                                "connection offer should be.")
    return status, body


def _follow(host: str, port: int, path: str, location: str) -> tuple[str, int, str]:
    """Work out where a redirect goes, refusing to leave the host."""
    if not location:
        raise ProvisioningError(f"{host} redirected to nowhere.")
    if location.startswith("/"):
        return host, port, location
    parts = urlsplit(location)
    if parts.scheme.lower() != "https":
        raise ProvisioningError(
            f"{host} redirected away from HTTPS, so nothing was sent."
        )
    if (parts.hostname or "").lower() != host or (parts.port or 443) != port:
        # Following this would mean the ticket - and the user's consent, which
        # named one host - arriving somewhere else entirely.
        raise ProvisioningError(
            f"{host} redirected to another site, so nothing was sent."
        )
    return host, port, parts.path + (f"?{parts.query}" if parts.query else "")


def _user_agent() -> str:
    from mysql_runner import __version__

    return f"Sitekeeper/{__version__} (Windows)"


def _explain(status: int, body: bytes) -> str:
    """Turn a failure into a sentence the customer can act on.

    A provider may send its own ``message``, and that is usually better than
    anything guessable from here - it knows whether the ticket was used or the
    account was suspended. It is still their text in our dialog, so it goes
    through :func:`clean_text` like every other borrowed string.
    """
    message = ""
    code = ""
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict):
            message = clean_text(data.get("message", ""), limit=300)
            code = clean_text(data.get("error", ""), limit=60)
    except (ValueError, UnicodeDecodeError):
        pass
    if message:
        return message
    if status in (401, 403):
        return (
            "This link is no longer valid. These are single-use and expire "
            "quickly - go back to your hosting panel and click the button again."
        )
    if status == 404:
        return "This provider does not offer Sitekeeper handover at that address."
    if status == 429:
        return "The provider asked us to slow down. Try again in a minute."
    if status >= 500:
        return "The provider's side is having trouble. Try again shortly."
    return f"The provider refused the request{f' ({code})' if code else ''}."


# ----- reading the payload ------------------------------------------------
def read_offer(data: object, *, source: str) -> Offer:
    """Validate a decoded payload and turn it into profiles.

    Separate from :func:`fetch` so the whole contract is testable without a
    network, which is also how a provider checks their own JSON.
    """
    if not isinstance(data, dict):
        raise ProvisioningError(f"{source} answered with something that is not "
                                "a connection offer.")
    version = data.get("version", CONTRACT_VERSION)
    if str(version) != str(CONTRACT_VERSION):
        raise ProvisioningError(
            f"{source} speaks version {clean_text(version, limit=12)} of the "
            "handover format, which this Sitekeeper does not know."
        )
    entries = data.get("connections")
    if not isinstance(entries, list) or not entries:
        raise ProvisioningError(f"{source} offered no connections.")
    if len(entries) > MAX_CONNECTIONS:
        raise ProvisioningError(
            f"{source} offered {len(entries)} connections, which is more than "
            f"the {MAX_CONNECTIONS} a single handover may carry."
        )

    offer = Offer(
        source=source,
        issuer=clean_text(data.get("issuer", "")) or source,
        expires_at=clean_text(data.get("expires_at", ""), limit=40),
    )
    dropped: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            offer.skipped.append(f"Connection {index} was not readable.")
            continue
        for name in REFUSED_FIELDS:
            if entry.get(name):
                dropped.add(name)
        try:
            profile, key = _profile_from(entry)
        except ProvisioningError as exc:
            offer.skipped.append(f"Connection {index}: {exc}")
            continue
        offer.profiles.append(profile)
        if key:
            offer.keys[profile.id] = key
    if not offer.profiles:
        detail = " " + " ".join(offer.skipped[:3]) if offer.skipped else ""
        raise ProvisioningError(
            f"Nothing in {source}'s offer could be used.{detail}"
        )
    offer.dropped = sorted(dropped)
    return offer


def _profile_from(entry: dict) -> tuple[ServerProfile, str]:
    """Build one profile from one payload entry, taking only what is allowed."""
    kind_name = str(entry.get("kind", "")).strip().lower()
    kind = _KINDS.get(kind_name)
    if kind is None:
        raise ProvisioningError(
            f"unsupported connection type {clean_text(kind_name, limit=20)!r}."
        )
    label = clean_text(entry.get("label", ""))
    profile = ServerProfile(label=label, kind=kind)
    profile.group = clean_text(entry.get("group", ""), limit=60)
    profile.environment = _environment(entry.get("environment"))
    profile.username = clean_text(entry.get("username", ""), limit=200)
    password = entry.get("password", "")
    profile.password = str(password) if isinstance(password, str) else ""

    if kind == ConnectionKind.PHPMYADMIN:
        url = str(entry.get("url", "")).strip()
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ProvisioningError("a phpMyAdmin connection needs an http(s) URL.")
        profile.url = url
        profile.auth_type = _auth_type(entry.get("auth_type"))
        profile.label = profile.label or urlsplit(url).netloc
        return profile, ""

    host = clean_text(entry.get("host", ""), limit=253)
    if not host or "/" in host or "@" in host:
        raise ProvisioningError("no usable host.")
    profile.host = host
    profile.port = _port(entry.get("port"))
    profile.label = profile.label or host

    if kind.is_sql:
        profile.database = clean_text(entry.get("database", ""), limit=120)
        if kind == ConnectionKind.MSSQL:
            # A named instance is a name, not a path or a command, so it
            # is cleaned the same way a label is and no further. Windows
            # authentication is deliberately not offered: a provider
            # cannot know this PC's accounts, and a connection that logs
            # in as whoever opens it is not theirs to arrange.
            profile.mssql_instance = clean_text(
                entry.get("instance", ""), limit=60
            )
            profile.mssql_encrypt = bool(entry.get("encrypt", True))
            profile.mssql_trust_cert = bool(entry.get("trust_certificate", True))
        return profile, ""

    remote_dir = clean_text(entry.get("remote_dir", ""), limit=400)
    profile.remote_dir = remote_dir.rstrip("/") if remote_dir != "/" else "/"
    profile.passive = bool(entry.get("passive", True))
    profile.ssh_port = _port(entry.get("ssh_port"))
    profile.use_agent = bool(entry.get("use_agent", True))
    profile.use_default_keys = bool(entry.get("use_default_keys", False))

    key = ""
    if kind == ConnectionKind.SFTP:
        key = _private_key(entry.get("private_key"))
    return profile, key


def _private_key(value: object) -> str:
    """Accept a PEM private key, or nothing. The caller writes it to disk."""
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").strip()
    if not text.startswith("-----BEGIN ") or "PRIVATE KEY-----" not in text:
        raise ProvisioningError("the private key offered is not in PEM form.")
    if len(text) > 64 * 1024:
        raise ProvisioningError("the private key offered is implausibly large.")
    return text + "\n"


def _port(value: object) -> int:
    try:
        port = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return port if 0 < port <= 65535 else 0


def _environment(value: object) -> Environment:
    try:
        return Environment(str(value or "none").strip().lower())
    except ValueError:
        return Environment.NONE


def _auth_type(value: object) -> AuthType:
    try:
        return AuthType(str(value or "auto").strip().lower())
    except ValueError:
        return AuthType.AUTO


def store_keys(offer: Offer) -> list[str]:
    """Write any provisioned private keys to disk and point profiles at them.

    Called after the user has accepted, never before: writing a stranger's key
    into the user's AppData is itself part of what they agreed to. Returns the
    problems, so a key that could not be written becomes a sentence rather than
    a connection that silently cannot authenticate.
    """
    problems: list[str] = []
    for profile in offer.profiles:
        pem = offer.keys.get(profile.id)
        if not pem:
            continue
        path = provisioned_key_path(profile.id)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(pem)
            os.chmod(path, 0o600)
        except OSError as exc:
            problems.append(f"{profile.label}: its key could not be saved ({exc}).")
            continue
        profile.private_key_path = str(path)
    return problems
