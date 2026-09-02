"""Data models for stored server profiles."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum


class ConnectionKind(str, Enum):
    """What kind of session a profile opens."""

    PHPMYADMIN = "phpmyadmin"  # Embedded browser tab with auto-login.
    MYSQL = "mysql"            # Native MySQL connection, CLI console tab.
    FTP = "ftp"                # Plain FTP file transfer.
    FTPS = "ftps"              # FTP over explicit TLS.
    SFTP = "sftp"              # SFTP over SSH.

    @property
    def is_transfer(self) -> bool:
        """Whether this kind opens the dual-pane file manager."""
        return self in (ConnectionKind.FTP, ConnectionKind.FTPS, ConnectionKind.SFTP)


class AuthType(str, Enum):
    """How a phpMyAdmin server expects credentials."""

    AUTO = "auto"          # Detect at runtime (cookie form, then HTTP basic).
    COOKIE = "cookie"      # phpMyAdmin cookie login form.
    HTTP_BASIC = "basic"   # HTTP Basic Authentication popup.


class Environment(str, Enum):
    """Environment level used for tab tinting / safety indicators."""

    NONE = "none"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


#: Port used when a profile leaves the port field at 0.
DEFAULT_PORTS = {
    ConnectionKind.MYSQL: 3306,
    ConnectionKind.FTP: 21,
    ConnectionKind.FTPS: 21,
    ConnectionKind.SFTP: 22,
}


@dataclass
class ServerProfile:
    """A saved connection: phpMyAdmin, native MySQL, or FTP/SFTP transfer."""

    label: str
    url: str = ""
    username: str = ""
    password: str = ""
    auth_type: AuthType = AuthType.AUTO
    group: str = ""
    environment: Environment = Environment.NONE
    startup_script: str = ""
    kind: ConnectionKind = ConnectionKind.PHPMYADMIN
    # Host-based kinds (MySQL / FTP / SFTP). Port 0 means "use the default".
    host: str = ""
    port: int = 0
    database: str = ""
    # Transfer kinds: where each pane starts, plus protocol specifics.
    remote_dir: str = ""
    local_dir: str = ""
    private_key_path: str = ""   # SFTP key-based auth (optional).
    passive: bool = True         # FTP/FTPS passive mode.
    #: Where this server's shell is, for the terminal and for server-side
    #: commands. FTP and FTPS have no shell of their own, so both borrow SSH
    #: on the same host with the same credentials. Zero means nobody has
    #: decided yet, which is what lets the app ask once - handing an FTP
    #: password to a different service is a decision, not a detail - and
    #: never ask again. SFTP ignores this and uses its own port.
    ssh_port: int = 0
    #: Offer the keys held by an SSH agent - Pageant, Windows' own ssh-agent,
    #: 1Password. On by default: plenty of accounts have no password at all
    #: and no key file to point at, and without this they cannot connect.
    use_agent: bool = True
    #: Also offer whatever is sitting in ~/.ssh. Off by default, because a
    #: server with a low MaxAuthTries can refuse the key that would have
    #: worked after three that were never meant for it.
    use_default_keys: bool = False
    #: Reach this server through another stored connection (its id). The
    #: bastion's credentials are the ones already in the vault, which is the
    #: whole reason for naming a profile rather than repeating a host here.
    jump_profile_id: str = ""
    #: Anything stranger: an OpenSSH-style ProxyCommand. %h, %p and %r are
    #: replaced with the host, port and username.
    proxy_command: str = ""
    #: Where this sits in its group when the list has been arranged by hand.
    #: Zero means "never dragged", and a group whose members are all zero is
    #: still listed alphabetically - so arranging one group by hand does not
    #: scramble the order of every other one.
    order: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def effective_port(self) -> int:
        """The port to dial, falling back to the protocol default."""
        return self.port or DEFAULT_PORTS.get(self.kind, 0)

    def describe_target(self) -> str:
        """Short human-readable destination, for tooltips and status lines."""
        if self.kind == ConnectionKind.PHPMYADMIN:
            return self.url
        target = f"{self.host}:{self.effective_port}"
        if self.kind == ConnectionKind.MYSQL:
            user = self.username or "?"
            suffix = f"/{self.database}" if self.database else ""
            return f"mysql://{user}@{target}{suffix}"
        return f"{self.kind.value}://{self.username or 'anonymous'}@{target}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["auth_type"] = self.auth_type.value
        data["environment"] = self.environment.value
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ServerProfile":
        # Every field beyond the original phpMyAdmin set is optional so that
        # vaults written by older versions load unchanged.
        return cls(
            id=data.get("id", uuid.uuid4().hex),
            label=data["label"],
            url=data.get("url", ""),
            username=data.get("username", ""),
            password=data.get("password", ""),
            auth_type=AuthType(data.get("auth_type", AuthType.AUTO.value)),
            group=data.get("group", ""),
            environment=Environment(data.get("environment", Environment.NONE.value)),
            startup_script=data.get("startup_script", ""),
            kind=ConnectionKind(data.get("kind", ConnectionKind.PHPMYADMIN.value)),
            host=data.get("host", ""),
            port=int(data.get("port", 0) or 0),
            database=data.get("database", ""),
            remote_dir=data.get("remote_dir", ""),
            local_dir=data.get("local_dir", ""),
            private_key_path=data.get("private_key_path", ""),
            passive=bool(data.get("passive", True)),
            ssh_port=int(data.get("ssh_port", 0) or 0),
            use_agent=bool(data.get("use_agent", True)),
            use_default_keys=bool(data.get("use_default_keys", False)),
            jump_profile_id=str(data.get("jump_profile_id", "") or ""),
            proxy_command=str(data.get("proxy_command", "") or ""),
            order=int(data.get("order", 0) or 0),
        )
