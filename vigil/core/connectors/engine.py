"""Connector Engine — the single engine for all plugin IO.

Handles all IO with external sources on behalf of pure plugins. It owns every
sub-connector domain — SSH (``connectors/ssh/``) and HTTP/DNS/ICMP
(``connectors/http/``) — and routes a plugin's declared, heterogeneous request
list to the right one. There is exactly one ConnectorEngine per VigilEngine; it
holds no per-plugin state.

A plugin declares ``requests() -> List[Request]`` (a mix of ``Command`` /
``HttpRequest`` / ``DnsQuery`` / ``PingRequest``) and consumes a
positionally-matched ``List[Result]`` in its pure ``parse_results()``.

The SSH domain differs from HTTP/DNS/ICMP in that it is *per target*: each
plugin talks to one host over a pooled asyncssh connection with its own
collect-timeout. That per-plugin identity is captured in a small ``SSHContext``
value (built once via :meth:`ConnectorEngine.ssh_context`) and passed back in
on each SSH call — so the engine itself stays a stateless singleton while still
reusing one connection per physical target.
"""

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from vigil.core.connectors.ssh.ssh import (
    SSHConnection, COLLECT_TIMEOUT, CONTROL_TIMEOUT,
)
from vigil.core.connectors.types import (
    ActionPlan, CmdResult, Command, DnsQuery, HttpRequest, PingRequest,
    Request, Result,
)
from vigil.core.settings.config_schema import PluginConfig

_SSHPoolKey = tuple  # (host, port, username, key_path)


@dataclass(frozen=True)
class SSHContext:
    """A plugin's per-target SSH handle: the pooled connection to its host plus
    the collect-cycle timeout for that plugin. Built once by
    ``ConnectorEngine.ssh_context`` and handed back to the engine on each SSH
    request, keeping the engine itself free of per-plugin state.

    ``target`` is the effective host the pool resolved the config to, exposed so
    the engine can keep a plugin's labels/reads in sync with what's collected."""
    conn: SSHConnection
    collect_timeout: float

    @property
    def target(self) -> str:
        return getattr(self.conn, 'host', '')


class ConnectorEngine:
    def __init__(self):
        # One shared asyncssh connection per physical target (host, port,
        # username, key_path), regardless of how many plugins point at it.
        self._ssh_conns: dict = {}
        # HTTP/DNS/ICMP sub-connectors are lazily built so importing this
        # package doesn't require requests / dnspython until those paths run.
        self._http = None
        self._dns = None
        self._icmp = None

    # --- SSH domain (per-target, via an SSHContext handle) ---

    def ssh_context(self, config: PluginConfig,
                    collect_timeout: float = COLLECT_TIMEOUT) -> SSHContext:
        """Build a plugin's SSH handle, reusing the pooled connection for its
        target. Construction is synchronous — SSHConnection.__init__ performs no
        I/O, only its execute() lazily connects."""
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
        return SSHContext(conn=conn, collect_timeout=collect_timeout)

    async def _ssh_exec(self, ctx: SSHContext, command: str,
                        timeout: Optional[float], default: float) -> CmdResult:
        """Run one command through the context's shared SSHConnection, falling
        back to `default` when the command carries no explicit timeout.
        SSHConnection.execute already catches asyncssh/OSError and returns
        (-1, "", err), so no wrapper try/except is needed here."""
        ret, out, err = await ctx.conn.execute(
            command, timeout=timeout if timeout is not None else default
        )
        return CmdResult(ret, out, err)

    async def _ssh_run(self, ctx: SSHContext, commands: List[Command]) -> List[CmdResult]:
        async def _one(cmd: Command) -> CmdResult:
            default = CONTROL_TIMEOUT if cmd.action else ctx.collect_timeout
            return await self._ssh_exec(ctx, cmd.text, cmd.timeout, default)

        return list(await asyncio.gather(*(_one(c) for c in commands)))

    async def execute(self, ctx: SSHContext, plan: ActionPlan) -> CmdResult:
        """Run a named action's short SSH command (control-cycle timeout)."""
        return await self._ssh_exec(ctx, plan.command, plan.timeout, CONTROL_TIMEOUT)

    async def execute_raw(self, ctx: SSHContext, command: str,
                          timeout: Optional[float] = None) -> CmdResult:
        """Escape hatch for one-off ad hoc commands (e.g. web-side dialogs or
        cancelling a detached job) that aren't modeled as a named action. Prefer
        plan_action()/execute() for anything reachable from action_id dispatch."""
        return await self.execute(ctx, ActionPlan(command, timeout=timeout))

    def close_ssh(self) -> None:
        for conn in self._ssh_conns.values():
            conn.close()
        self._ssh_conns.clear()

    # --- HTTP / DNS / ICMP domains (engine-wide shared sub-connectors) ---

    @property
    def http(self):
        if self._http is None:
            from vigil.core.connectors.http.http_connector import HttpConnector
            self._http = HttpConnector()
        return self._http

    @property
    def dns(self):
        if self._dns is None:
            from vigil.core.connectors.http.dns_connector import DnsConnector
            self._dns = DnsConnector()
        return self._dns

    @property
    def icmp(self):
        if self._icmp is None:
            from vigil.core.connectors.http.icmp_connector import IcmpConnector
            self._icmp = IcmpConnector()
        return self._icmp

    # --- Unified routing ---

    async def run(self, ssh_ctx: Optional[SSHContext],
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
