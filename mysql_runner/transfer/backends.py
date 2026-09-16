"""Turn a stored profile into the backend that talks to it.

There were three copies of this wiring: the app's ConnectionSpec, the MCP
server's ``_build``, and - the moment anything else needed to open a
connection - a fourth. The MCP copy even said why, in a comment: the app's
version lives next to Qt and so could not be imported from a headless
process. That is the only thing this module changes. Nothing here imports Qt,
so there is one answer to "how is this profile dialled" and every caller uses
it.

Two decisions travel with the caller rather than with the profile, because
they are about the caller and not about the server:

* **the host key policy.** The application stops and asks before trusting a
  server it has never seen (``hostkeys.PROMPT``); a headless caller has
  nobody to ask and records it instead (``hostkeys.AUTO``). Getting this
  backwards either hangs a background process or turns the one identity check
  SSH has into no check at all, so it is never defaulted quietly here.
* **the jump host**, which has to be resolved from the vault by whoever has
  the vault open, and arrives already looked up.
"""

from __future__ import annotations

from mysql_runner.storage.models import ConnectionKind
from mysql_runner.transfer import hostkeys
from mysql_runner.transfer.base import RemoteFS


def build(
    kind: ConnectionKind,
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    private_key_path: str = "",
    passive: bool = True,
    use_agent: bool = True,
    use_default_keys: bool = False,
    host_key_mode: str = hostkeys.PROMPT,
    jump: object = None,
    proxy_command: str = "",
) -> RemoteFS:
    """The backend for one endpoint. Imports the driver, does not connect."""
    if kind == ConnectionKind.SFTP:
        from mysql_runner.transfer.sftp_client import SFTPFileSystem

        return SFTPFileSystem(
            host,
            port,
            username,
            password,
            private_key_path=private_key_path,
            use_agent=use_agent,
            use_default_keys=use_default_keys,
            host_key_mode=host_key_mode,
            jump=jump,
            proxy_command=proxy_command,
        )
    from mysql_runner.transfer.ftp_client import FTPFileSystem

    return FTPFileSystem(
        host,
        port,
        username,
        password,
        use_tls=kind == ConnectionKind.FTPS,
        passive=passive,
    )


def jump_for(profile, lookup) -> tuple[object, str]:
    """Resolve the bastion a connection goes through: (jump, complaint).

    ``lookup`` takes a profile id and returns that profile or None - the
    vault in the app, a list in a dialog that has not saved anything yet.

    A named jump host that has since been deleted or turned into something
    that cannot forward is refused rather than ignored. Connecting straight
    at a server somebody deliberately put behind a bastion is not a smaller
    version of what they asked for - it is a different thing, and on a
    private network it would only fail with a confusing timeout anyway.

    One hop. Chains are not followed: the bastion is reached directly, even
    if it names a jump host of its own.
    """
    wanted = getattr(profile, "jump_profile_id", "")
    if not wanted:
        return None, ""
    bastion = lookup(wanted)
    if bastion is None:
        return None, (
            f"{profile.label} is set to connect through another saved "
            "connection, but that connection no longer exists. Edit "
            f"{profile.label} and pick a jump host, or clear the setting."
        )
    if bastion.kind != ConnectionKind.SFTP:
        return None, (
            f"{profile.label} is set to connect through {bastion.label}, "
            f"which is a {bastion.kind.value} connection. Only SFTP "
            "connections can forward for another."
        )
    from mysql_runner.transfer.sftp_client import JumpHost

    return JumpHost(
        host=bastion.host,
        port=bastion.effective_port,
        username=bastion.username,
        password=bastion.password,
        private_key_path=bastion.private_key_path,
        use_agent=bastion.use_agent,
        label=bastion.label,
    ), ""


def for_profile(
    profile,
    *,
    jump: object = None,
    host_key_mode: str = hostkeys.PROMPT,
) -> RemoteFS:
    """The backend a stored profile describes."""
    return build(
        profile.kind,
        profile.host,
        profile.effective_port,
        profile.username,
        profile.password,
        private_key_path=profile.private_key_path,
        passive=profile.passive,
        use_agent=profile.use_agent,
        use_default_keys=profile.use_default_keys,
        host_key_mode=host_key_mode,
        jump=jump,
        proxy_command=profile.proxy_command,
    )
