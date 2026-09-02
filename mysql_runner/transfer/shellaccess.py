"""Where a saved connection's shell is, including the ones that have none.

A terminal used to be something only an SFTP session could offer, and only
from inside a file-manager tab: the shell lived behind *Server tools*, which
is hidden on FTP because FTP has no shell. That is true of the protocol and
false of the server. Almost every host reached over FTP also answers on SSH,
with the same account - it is how the files got there in the first place - so
"this connection cannot have a terminal" was an answer about a wire format,
not about the machine at the other end.

So the shell is resolved from the profile rather than from the live session:

* **SFTP** shells on its own port. Nothing to decide.
* **FTP and FTPS** borrow SSH on the same host, same username, same password,
  on :data:`DEFAULT_SSH_PORT` unless the profile names another.

The borrowing is deliberately not silent. Sending an FTP password to a
different service on that host is a decision somebody should make once, with
their eyes open, so ``ssh_port`` starts at zero meaning "nobody has said" -
:func:`borrows_credentials` is what the UI asks to know whether to put the
question. Once answered it is stored on the profile and never asked again.

Everything here is plain data on plain data: no connection is opened, so both
the app and the headless MCP server can use the same answer.
"""

from __future__ import annotations

from dataclasses import replace

from mysql_runner.storage.models import ConnectionKind, ServerProfile

#: Where SSH is when nobody has said otherwise.
DEFAULT_SSH_PORT = 22


def has_shell(profile: ServerProfile) -> bool:
    """Whether a terminal is possible for this connection at all.

    True for every file-transfer profile - which is the change: an FTP
    connection is a server you can get a shell on, not a protocol that has
    none. Whether the *login* works is the server's answer to give, not
    something to guess at from here.
    """
    return profile.kind.is_transfer


def borrows_credentials(profile: ServerProfile) -> bool:
    """Whether a shell here means handing these credentials to SSH instead.

    True for FTP and FTPS, and only until the profile records a port: the
    question is asked once per connection, not once per terminal.
    """
    return profile.kind in (ConnectionKind.FTP, ConnectionKind.FTPS)


def shell_port(profile: ServerProfile) -> int:
    """The port this connection's shell answers on."""
    if profile.kind == ConnectionKind.SFTP:
        return profile.effective_port
    return profile.ssh_port or DEFAULT_SSH_PORT


def shell_profile(profile: ServerProfile) -> ServerProfile:
    """The same connection, described as the SSH session a shell needs.

    A copy rather than a mutation, and an ordinary :class:`ServerProfile`
    rather than a new kind of object, so everything that already knows how to
    open an SFTP session - the app's ConnectionSpec, the MCP server's own
    backend builder - opens this one with no special case anywhere.
    """
    if profile.kind == ConnectionKind.SFTP:
        return profile
    return replace(profile, kind=ConnectionKind.SFTP, port=shell_port(profile))
