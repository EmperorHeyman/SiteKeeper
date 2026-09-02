"""Deciding whether the server answering is the one that answered last time.

SSH has no certificate authority. The only thing standing between you and a
machine impersonating your server is the host key: you see it once, you
remember it, and you are told if it ever changes. Sitekeeper used to do the
remembering and skip the seeing - ``AutoAddPolicy`` records whatever answers
the first time without a word - so the one moment the check exists for, the
first connection, was the one moment nothing was checked.

That is a strange gap in an application whose whole manner is to ask before
anything irreversible. It now asks: an unknown key stops the connection and
comes back as :class:`HostKeyUnknown`, carrying the fingerprint to show and the
key to store if the answer is yes. A *changed* key still fails outright and
always will - that is not a question worth putting to somebody in a hurry.

Headless callers (the MCP server, the FastAPI backend) keep the old behaviour,
because there is nobody there to ask and refusing would only mean a tool that
cannot connect at all. See :data:`AUTO` and :data:`PROMPT`.
"""

from __future__ import annotations

import base64
import hashlib

from mysql_runner.paths import known_hosts_path
from mysql_runner.transfer.base import TransferError

#: Record an unknown key and carry on - what a headless caller must do.
AUTO = "auto"
#: Stop, and let the caller ask. What the application does.
PROMPT = "prompt"

#: The port that needs no mention in a known_hosts entry.
DEFAULT_PORT = 22


class HostKeyUnknown(TransferError):
    """This host has never been seen before, and nobody has vouched for it.

    Carries everything needed both to show the user what they are agreeing to
    and to record the answer, so the caller does not have to reconnect to find
    out what it was being asked about.
    """

    def __init__(self, host: str, port: int, key) -> None:
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.key = key
        self.key_type = friendly_type(key.get_name())
        self.sha256 = fingerprint_sha256(key)
        self.md5 = fingerprint_md5(key)
        self.bits = getattr(key, "get_bits", lambda: 0)() or 0
        super().__init__(
            f"{host} has not been connected to before, so its identity has "
            f"not been confirmed ({self.key_type} {self.sha256})."
        )


def entry_name(host: str, port: int) -> str:
    """How OpenSSH - and Paramiko - name a host in known_hosts."""
    port = int(port or DEFAULT_PORT)
    return host if port == DEFAULT_PORT else f"[{host}]:{port}"


def fingerprint_sha256(key) -> str:
    """``SHA256:2ip...`` - the fingerprint every modern ssh client prints."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def fingerprint_md5(key) -> str:
    """``MD5:16:27:...`` - the older form, still what many panels display.

    Worth showing beside the SHA-256 one: the value being compared against is
    often a hosting control panel that has not been updated in a decade, and a
    fingerprint you cannot find in the format you have is no check at all.
    """
    digest = hashlib.md5(key.asbytes()).digest()  # noqa: S324 - a fingerprint, not a secret
    return "MD5:" + ":".join(f"{byte:02x}" for byte in digest)


def friendly_type(name: str) -> str:
    """``ssh-ed25519`` -> ``ED25519``, and so on."""
    cleaned = (name or "").removeprefix("ssh-").replace("ecdsa-sha2-", "ECDSA ")
    return cleaned.upper() or "unknown"


def trust(host: str, port: int, key) -> None:
    """Record a host key, so this connection is not questioned again."""
    import paramiko

    path = known_hosts_path()
    store = paramiko.HostKeys()
    if path.exists():
        try:
            store.load(str(path))
        except (OSError, paramiko.SSHException):
            # A corrupt file must not stop somebody trusting a server; what is
            # unreadable is rewritten rather than kept.
            store = paramiko.HostKeys()
    store.add(entry_name(host, port), key.get_name(), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.save(str(path))


def is_known(host: str, port: int) -> bool:
    """Whether anything has been recorded for this host at all."""
    import paramiko

    path = known_hosts_path()
    if not path.exists():
        return False
    store = paramiko.HostKeys()
    try:
        store.load(str(path))
    except (OSError, paramiko.SSHException):
        return False
    return store.lookup(entry_name(host, port)) is not None


def forget(host: str, port: int) -> bool:
    """Drop what is recorded for one host. True when something went.

    The way back from a server that was legitimately rekeyed - which used to
    mean opening known_hosts in a text editor and finding the right line.
    """
    import paramiko

    path = known_hosts_path()
    if not path.exists():
        return False
    store = paramiko.HostKeys()
    try:
        store.load(str(path))
    except (OSError, paramiko.SSHException):
        return False
    name = entry_name(host, port)
    if store.lookup(name) is None:
        return False
    # HostKeys has no remove(); rebuild without the entries naming this host.
    kept = paramiko.HostKeys()
    for entry in store._entries:  # noqa: SLF001 - the only way in
        if name not in entry.hostnames:
            for hostname in entry.hostnames:
                kept.add(hostname, entry.key.get_name(), entry.key)
    kept.save(str(path))
    return True


def policy(mode: str):
    """The Paramiko missing-host-key policy for ``AUTO`` or ``PROMPT``."""
    import paramiko

    if mode == AUTO:
        return paramiko.AutoAddPolicy()

    class _AskFirst(paramiko.MissingHostKeyPolicy):
        """Stop and hand the question back to whoever asked to connect."""

        def missing_host_key(self, client, hostname, key):
            port = DEFAULT_PORT
            transport = client.get_transport()
            if transport is not None:
                # The hostname Paramiko passes is already "[host]:port" for a
                # non-default port; the port itself has to come from the socket.
                try:
                    port = transport.getpeername()[1]
                except Exception:
                    pass
            raise HostKeyUnknown(_bare_host(hostname), port, key)

    return _AskFirst()


def _bare_host(hostname: str) -> str:
    """``[example.com]:2202`` -> ``example.com``."""
    if hostname.startswith("[") and "]" in hostname:
        return hostname[1:hostname.index("]")]
    return hostname
