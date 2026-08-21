"""Agent connector — the server half of the Vigil agent transport.

An agent dials in over a WebSocket and stays connected. This module holds
the server-side view of that link:

* :class:`AgentConnection` — one connected (or currently absent) agent. Its
  ``execute()`` has the same ``(exit_code, stdout, stderr)`` shape as
  ``SSHConnection.execute()``, so the Connector Engine can hand a plugin an
  agent-backed handle and every existing plugin runs unchanged.
* :class:`AgentRegistry` — the id -> connection map, plus the event fan-out
  to whichever monitor subscribed to a stream.

An agent that is configured but not currently connected is not an error at
construction time: the connection object exists from startup and fails each
call with a clear message until the agent dials in. That keeps the monitor
schedule identical whether the target is up or down, exactly as a refused
SSH dial does today.

Unlike SSH there is no per-host session ceiling here — commands are frames
multiplexed on one socket, so concurrency is bounded only by what the target
can usefully run at once.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from vigil.core.connectors import agent_protocol as proto
from vigil.core.connectors.agent_protocol import StreamSpec

EXEC_TIMEOUT = 30.0
CONTROL_TIMEOUT = 60.0

_DISCONNECTED_GRACE = 0.0
"""Seconds to wait for an absent agent to reconnect before failing a call.
Zero: fail fast, because a monitor's next cycle retries anyway and a queued
command against a dead agent only delays the schedule."""


class AgentConnection:
    """One agent's server-side endpoint. Created at config load and reused for
    the process lifetime; the underlying socket comes and goes as the agent
    reconnects, and in-flight calls are failed rather than stranded when it
    drops."""

    def __init__(self, agent_id: str, host: str):
        self.agent_id = agent_id
        self.host = host
        self.hostname: Optional[str] = None
        self.version: Optional[str] = None
        self.capabilities: List[str] = []
        self.connected_at: Optional[float] = None
        self._socket: Any = None
        self._send_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._streams: Dict[str, StreamSpec] = {}

    # --- Liveness ---

    @property
    def is_connected(self) -> bool:
        return self._socket is not None

    def attach(self, socket: Any, hello: Dict[str, Any]) -> None:
        """Bind a freshly authenticated socket. A second agent claiming the
        same id replaces the first — a reconnect after a network drop often
        arrives before the old socket has been noticed as dead."""
        if self._socket is not None:
            logging.warning(
                f"agent {self.agent_id!r}: replacing an existing connection "
                f"(the previous socket is assumed dead)"
            )
            self._fail_pending("Superseded by a new agent connection")
        self._socket = socket
        self.hostname = hello.get('hostname')
        self.version = hello.get('version')
        caps = hello.get('caps')
        self.capabilities = [str(c) for c in caps] if isinstance(caps, list) else []
        self.connected_at = time.time()
        logging.info(
            f"agent {self.agent_id!r} connected from {self.hostname or self.host} "
            f"(version {self.version or 'unknown'})"
        )

    def detach(self, socket: Any) -> None:
        """Drop the socket if it is still the current one. The identity check
        matters: a slow teardown of a superseded socket must not unbind the
        replacement that already took its place."""
        if self._socket is not socket:
            return
        self._socket = None
        self.connected_at = None
        self._fail_pending("Agent disconnected")
        logging.info(f"agent {self.agent_id!r} disconnected")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_result((-1, "", reason))
        self._pending.clear()

    # --- Exec RPC (the SSHConnection-shaped surface) ---

    async def execute(self, command: str,
                      timeout: float = EXEC_TIMEOUT) -> Tuple[int, str, str]:
        """Run one command on the agent's host. Mirrors
        ``SSHConnection.execute``: any transport failure or timeout maps to
        ``(-1, "", message)`` rather than raising, so plugin parse code sees
        one uniform failure shape regardless of transport."""
        socket = self._socket
        if socket is None:
            return -1, "", f"Agent {self.agent_id!r} is not connected"

        request_id = self._next_id = self._next_id + 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(proto.exec_request(request_id, command, timeout))
        except Exception as e:
            self._pending.pop(request_id, None)
            return -1, "", f"Agent send failed: {e}"

        try:
            # The agent enforces `timeout` on its side and replies with a
            # timeout result; this slightly longer wait only covers the case
            # where the agent itself stops answering, and is what stops a
            # monitor's cycle hanging on a wedged agent.
            return await asyncio.wait_for(future, timeout=timeout + 5.0)
        except asyncio.TimeoutError:
            return -1, "", f"Agent did not answer within {timeout + 5.0}s"
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: int, exit_code: int, stdout: str, stderr: str) -> None:
        """Complete the call a RESULT frame belongs to. A result for an
        unknown id (a late reply to an already-timed-out call) is dropped."""
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result((exit_code, stdout, stderr))

    async def _send(self, frame: Dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("not connected")
        async with self._send_lock:
            await socket.send_text(proto.encode(frame))

    # --- Event streams ---

    def register_stream(self, spec: StreamSpec) -> None:
        self._streams[spec.id] = spec

    @property
    def streams(self) -> List[StreamSpec]:
        return list(self._streams.values())

    async def push_subscriptions(self) -> None:
        """Send the agent its full stream set. Always the complete set rather
        than a delta, so a reconnecting agent converges on the right state
        without the server tracking what it already knows."""
        if self._socket is None:
            return
        try:
            await self._send(proto.subscribe([s.to_wire() for s in self._streams.values()]))
        except Exception as e:
            logging.warning(f"agent {self.agent_id!r}: could not send subscriptions: {e}")

    def close(self) -> None:
        """Match SSHConnection.close() so the Connector Engine can tear down
        either transport identically."""
        self._fail_pending("Vigil is shutting down")
        self._socket = None


class AgentRegistry:
    """Every configured agent, and the routing of inbound event frames.

    Connections are created eagerly from config so a plugin can be wired to an
    agent that has not dialled in yet; the plugin simply reports failed until
    it does."""

    def __init__(self):
        self._agents: Dict[str, AgentConnection] = {}
        self._tokens: Dict[str, str] = {}
        self._event_sink: Optional[Callable[[str, str, float, Dict[str, Any]], None]] = None

    def configure(self, agents_cfg: List[Dict[str, Any]]) -> None:
        """Build the connection objects from config.yaml's ``agents:`` list.
        An entry with no id is skipped with a log rather than raising, matching
        how a malformed plugin entry degrades."""
        for entry in agents_cfg or []:
            agent_id = entry.get('id') or entry.get('name')
            if not agent_id:
                logging.error(f"agents: entry with no `id` ignored: {entry!r}")
                continue
            agent_id = str(agent_id)
            token = self._resolve_token(agent_id, entry)
            if not token:
                logging.error(
                    f"agent {agent_id!r} has no usable token and will refuse "
                    f"every connection — set `token` or `token_file`"
                )
            else:
                self._tokens[agent_id] = token
            self._agents[agent_id] = AgentConnection(
                agent_id, host=str(entry.get('host') or agent_id)
            )
            logging.info(f"Registered agent {agent_id!r}")

    @staticmethod
    def _resolve_token(agent_id: str, entry: Dict[str, Any]) -> Optional[str]:
        """Take the token from `token_file` if given, else `token`.

        `token_file` is the deployment-friendly form and the one to prefer: the
        secret stays with the secret manager and never enters config.yaml,
        which under Nix (and any config-generating deployment) is world
        readable. Read once at startup, like auth's `password_file`."""
        token_file = entry.get('token_file')
        if token_file:
            try:
                return Path(str(token_file)).read_text(encoding='utf-8').strip() or None
            except OSError as e:
                logging.error(
                    f"agent {agent_id!r}: could not read token_file {token_file}: {e}"
                )
                return None
        token = entry.get('token')
        return str(token) if token else None

    def get(self, agent_id: str) -> Optional[AgentConnection]:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> AgentConnection:
        """Return the named agent's connection, creating a placeholder for an
        unconfigured id. A plugin pointing at an agent that is missing from
        `agents:` is a config error the operator should see as a failing
        monitor with an explicit message, not a crash at startup."""
        conn = self._agents.get(agent_id)
        if conn is None:
            logging.error(
                f"Monitor references agent {agent_id!r}, which is not declared "
                f"in the top-level `agents:` config — it can never connect"
            )
            conn = self._agents[agent_id] = AgentConnection(agent_id, host=agent_id)
        return conn

    def token_for(self, agent_id: str) -> Optional[str]:
        return self._tokens.get(agent_id)

    @property
    def all(self) -> List[AgentConnection]:
        return list(self._agents.values())

    # --- Event fan-out ---

    def set_event_sink(self, sink: Callable[[str, str, float, Dict[str, Any]], None]) -> None:
        """Install the callback inbound events are handed to, as
        ``(agent_id, stream_id, timestamp, payload)``. The Coordination Engine
        owns it; the registry stays free of plugin knowledge."""
        self._event_sink = sink

    def dispatch_event(self, agent_id: str, stream_id: str,
                       timestamp: float, payload: Dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(agent_id, stream_id, timestamp, payload)
        except Exception as e:
            logging.error(f"agent {agent_id!r}: event handler failed for stream {stream_id!r}: {e}")

    def close(self) -> None:
        for conn in self._agents.values():
            conn.close()
