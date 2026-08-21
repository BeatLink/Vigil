"""Connector Engine — the single engine for all plugin IO.

Handles all IO with external sources on behalf of pure plugins. It owns every
sub-connector domain — SSH (``ssh_connector``) and HTTP/DNS/ICMP
(``http_connector`` / ``dns_connector`` / ``icmp_connector``) — and routes a
plugin's declared, heterogeneous request list to the right one. There is exactly
one ConnectorEngine per VigilEngine; it holds no per-plugin state.

A plugin declares ``requests() -> List[Request]`` (a mix of ``Command`` /
``HttpRequest`` / ``DnsQuery`` / ``PingRequest``) and consumes a
positionally-matched ``List[Result]`` in its pure ``parse_results()``.

The shell domain differs from HTTP/DNS/ICMP in that it is *per target*: each
plugin talks to one host with its own collect-timeout. That per-plugin identity
is captured in a small ``ExecContext`` value (built once via
:meth:`ConnectorEngine.exec_context`) and passed back in on each ``Command``
call — so the engine itself stays a stateless singleton while still reusing one
connection per physical target.

An ``ExecContext`` wraps whichever transport reaches that target: a pooled
asyncssh connection, or a connected Vigil agent when the monitor declares
``agent:``. Both expose the same ``execute(command, timeout) ->
(exit_code, stdout, stderr)``, so switching a monitor between them is a config
change and no plugin code is transport-aware.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional

from vigil.core.connectors.agent_connector import AgentConnection, AgentRegistry
from vigil.core.connectors.ssh_connector import (
    SSHConnection, COLLECT_TIMEOUT, CONTROL_TIMEOUT,
)
from vigil.core.connectors.types import (
    ActionPlan, CmdResult, Command, DnsQuery, HttpRequest, PingRequest,
    Request, Result,
)
from vigil.core.settings.config_schema import PluginConfig

_SSHPoolKey = tuple  # (host, port, username, key_path)


@dataclass(frozen=True)
class ExecContext:
    """A plugin's per-target command handle: the connection to its host plus
    the collect-cycle timeout for that plugin. Built once by
    ``ConnectorEngine.exec_context`` and handed back to the engine on each
    ``Command`` request, keeping the engine itself free of per-plugin state.

    ``conn`` is an ``SSHConnection`` or an ``AgentConnection`` — the two share
    an ``execute()`` signature and a ``host`` attribute, which is the whole
    reason a plugin never learns which one it is running over.

    ``target`` is the effective host the transport resolved the config to,
    exposed so the engine can keep a plugin's labels/reads in sync with what's
    collected."""
    conn: Any
    collect_timeout: float

    @property
    def target(self) -> str:
        return getattr(self.conn, 'host', '')

    @property
    def is_agent(self) -> bool:
        return isinstance(self.conn, AgentConnection)


# The transport-neutral name is ExecContext; SSHContext stays as an alias
# because it is the name the rest of the tree and the test suite import.
SSHContext = ExecContext


class ConnectorEngine:
    def __init__(self):
        # One shared asyncssh connection per physical target (host, port,
        # username, key_path), regardless of how many plugins point at it.
        self._ssh_conns: dict = {}
        # Every configured Vigil agent. Connections are created at config load
        # and live for the process; the socket underneath comes and goes.
        self.agents = AgentRegistry()
        # HTTP/DNS/ICMP sub-connectors are lazily built so importing this
        # package doesn't require requests / dnspython until those paths run.
        self._http = None
        self._dns = None
        self._icmp = None

    # --- Command domain (per-target, via an ExecContext handle) ---

    def exec_context(self, config: PluginConfig,
                     collect_timeout: float = COLLECT_TIMEOUT) -> ExecContext:
        """Build a plugin's command handle for its target, picking the
        transport from config: ``agent: <id>`` routes over that agent's
        WebSocket, anything else over the pooled SSH connection.

        Construction is synchronous and dials nothing — an SSHConnection
        connects lazily on first execute(), and an AgentConnection waits for
        the agent to dial in."""
        agent_id = config.get('agent')
        if agent_id:
            return ExecContext(conn=self.agents.require(str(agent_id)),
                               collect_timeout=collect_timeout)

        ssh_cfg = config.get('ssh_config', {})
        host = ssh_cfg.get('host', config.get('target_host', 'localhost'))
        key: _SSHPoolKey = (
            host,
            ssh_cfg.get('port') or 22,
            ssh_cfg.get('username'),
            ssh_cfg.get('key_path'),
        )
        conn = self._ssh_conns.get(key)
        if conn is None:
            conn = SSHConnection.from_config(config)
            self._ssh_conns[key] = conn
        return ExecContext(conn=conn, collect_timeout=collect_timeout)

    # The pre-agent name for the above, kept so existing call sites and tests
    # that only ever meant "give me this plugin's command handle" still work.
    ssh_context = exec_context

    async def _ssh_exec(self, ctx: ExecContext, command: str,
                        timeout: Optional[float], default: float) -> CmdResult:
        """Run one command through the context's connection, falling back to
        `default` when the command carries no explicit timeout. Both transports
        already map their own failures to (-1, "", err), so no wrapper
        try/except is needed here."""
        ret, out, err = await ctx.conn.execute(
            command, timeout=timeout if timeout is not None else default
        )
        return CmdResult(ret, out, err)

    async def _ssh_run(self, ctx: ExecContext, commands: List[Command]) -> List[CmdResult]:
        async def _one(cmd: Command) -> CmdResult:
            default = CONTROL_TIMEOUT if cmd.action else ctx.collect_timeout
            return await self._ssh_exec(ctx, cmd.text, cmd.timeout, default)

        return list(await asyncio.gather(*(_one(c) for c in commands)))

    async def execute(self, ctx: ExecContext, plan: ActionPlan) -> CmdResult:
        """Run a named action's short command (control-cycle timeout)."""
        return await self._ssh_exec(ctx, plan.command, plan.timeout, CONTROL_TIMEOUT)

    async def execute_raw(self, ctx: ExecContext, command: str,
                          timeout: Optional[float] = None) -> CmdResult:
        """Escape hatch for one-off ad hoc commands (e.g. web-side dialogs or
        cancelling a detached job) that aren't modeled as a named action. Prefer
        plan_action()/execute() for anything reachable from action_id dispatch."""
        return await self.execute(ctx, ActionPlan(command, timeout=timeout))

    def close_ssh(self) -> None:
        for conn in self._ssh_conns.values():
            conn.close()
        self._ssh_conns.clear()
        self.agents.close()

    # --- HTTP / DNS / ICMP domains (engine-wide shared sub-connectors) ---

    @property
    def http(self):
        if self._http is None:
            from vigil.core.connectors.http_connector import HttpConnector
            self._http = HttpConnector()
        return self._http

    @property
    def dns(self):
        if self._dns is None:
            from vigil.core.connectors.dns_connector import DnsConnector
            self._dns = DnsConnector()
        return self._dns

    @property
    def icmp(self):
        if self._icmp is None:
            from vigil.core.connectors.icmp_connector import IcmpConnector
            self._icmp = IcmpConnector()
        return self._icmp

    # --- Unified routing ---

    async def run(self, ssh_ctx: Optional[ExecContext],
                  requests: List[Request]) -> List[Result]:
        """Route each request to its sub-connector and gather the results,
        preserving order so parse_results() can match by position.

        ``ssh_ctx`` is the plugin's SSH handle (needed only for ``Command``
        requests); pass None for plugins that issue no SSH."""

        async def _one(req: Request) -> Result:
            if isinstance(req, Command):
                return (await self._ssh_run(ssh_ctx, [req]))[0]
            if isinstance(req, HttpRequest):
                return await self.http.fetch(req)
            if isinstance(req, DnsQuery):
                return await self.dns.resolve(req)
            if isinstance(req, PingRequest):
                return await self.icmp.ping(req)
            raise TypeError(f"Unroutable request type: {type(req).__name__}")

        return list(await asyncio.gather(*(_one(r) for r in requests)))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        self.close_ssh()
