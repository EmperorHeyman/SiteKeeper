"""Keys held by an SSH agent - including the agent Paramiko cannot reach.

Plenty of people never type an SSH password and never point a client at a key
file: the key lives in an agent, unlocked once at login, and every client asks
the agent to sign for it. Until now Sitekeeper asked for neither - it connected
with ``allow_agent=False`` - so anyone whose key is in Pageant, in Windows'
own ssh-agent, or behind 1Password's agent simply could not connect at all.

Paramiko can talk to Pageant on Windows and to ``SSH_AUTH_SOCK`` elsewhere, but
not to the agent that ships *with Windows*, which listens on a named pipe
rather than a socket. That one is filled in here: the agent protocol is the
same either way, so only the transport underneath it has to be written, and
Paramiko's own ``AgentSSH`` does the talking.

Both are then presented as one agent, because "which agent is it in" is not a
question anybody wants to answer to connect to a server.

Qt-free, and safe to import where Paramiko is missing - nothing here imports it
until it is asked to.
"""

from __future__ import annotations

import sys

#: Where the OpenSSH agent that ships with Windows listens. Paramiko looks for
#: Pageant and for a Unix socket, and finds neither of them here.
WINDOWS_AGENT_PIPE = r"\\.\pipe\openssh-ssh-agent"


class _PipeConnection:
    """A Windows named pipe dressed as the socket Paramiko's agent expects.

    The agent protocol is length-prefixed messages in both directions, which a
    byte-mode pipe carries exactly as a socket would; ``send`` and ``recv`` are
    the only two methods ``AgentSSH`` ever calls, plus ``close``.
    """

    def __init__(self, path: str) -> None:
        # Unbuffered, so a write is a message and a read does not sit waiting
        # for a buffer to fill that the agent will never send more into.
        self._pipe = open(path, "r+b", buffering=0)

    def send(self, data: bytes) -> int:
        return self._pipe.write(data)

    def recv(self, size: int) -> bytes:
        return self._pipe.read(size) or b""

    def close(self) -> None:
        try:
            self._pipe.close()
        except OSError:
            pass


def _pipe_agent(path: str = WINDOWS_AGENT_PIPE):
    """The Windows OpenSSH agent, or None when it is not running."""
    if not sys.platform.startswith("win"):
        return None
    from paramiko.agent import AgentSSH

    class PipeAgent(AgentSSH):
        """Paramiko's agent client, speaking over a named pipe."""

        def __init__(self, pipe_path: str) -> None:
            AgentSSH.__init__(self)
            self._connect(_PipeConnection(pipe_path))

        def close(self) -> None:
            self._close()

    try:
        return PipeAgent(path)
    except Exception:
        # Not running, not permitted, or not speaking the protocol. An agent
        # that is not there is the ordinary case, not an error.
        return None


def _paramiko_agent():
    """Pageant on Windows, ``SSH_AUTH_SOCK`` elsewhere, or None."""
    try:
        from paramiko.agent import Agent

        agent = Agent()
    except Exception:
        return None
    return agent if agent.get_keys() else _closed(agent)


def _closed(agent):
    """Close an agent that turned out to hold nothing, and return None."""
    try:
        agent.close()
    except Exception:
        pass
    return None


class CombinedAgent:
    """Every agent this machine has, behind the interface Paramiko expects.

    ``SSHClient`` builds its own ``Agent()`` only when ``client._agent`` is
    still None, so handing it one of these is all it takes for the keys below
    to be offered during authentication, in order, exactly as its own would be.
    """

    def __init__(self, agents: list, names: list[str]) -> None:
        self._agents = agents
        self._names = names

    def get_keys(self):
        return tuple(key for agent in self._agents for key in agent.get_keys())

    def close(self) -> None:
        for agent in self._agents:
            try:
                agent.close()
            except Exception:
                pass
        self._agents = []

    def describe(self) -> str:
        """"Pageant (2 keys)" - for the message when authentication fails."""
        parts = []
        for name, agent in zip(self._names, self._agents):
            count = len(agent.get_keys())
            parts.append(f"{name} ({count} key{'s' if count != 1 else ''})")
        return ", ".join(parts)


def open_agent() -> CombinedAgent | None:
    """Every agent holding keys on this machine, or None if there are none.

    The caller closes what it gets. Returning None rather than an empty agent
    matters: it is what lets the caller say "no agent is running" instead of
    "the agent refused you", which are different problems with different fixes.
    """
    agents, names = [], []
    for name, agent in (
        ("Pageant / SSH_AUTH_SOCK", _paramiko_agent()),
        ("Windows OpenSSH agent", _pipe_agent()),
    ):
        if agent is not None and agent.get_keys():
            agents.append(agent)
            names.append(name)
        elif agent is not None:
            _closed(agent)
    if not agents:
        return None
    return CombinedAgent(agents, names)
