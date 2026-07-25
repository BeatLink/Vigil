import asyncio
from typing import Any, Dict, List, Optional, Tuple

from vigil.core.connectors.ssh.ssh import SSHConnection, SSHCollector, SSHController
from vigil.core.connectors.types import ActionPlan, CmdResult, Command
from vigil.core.settings.config_schema import PluginConfig

_PoolKey = Tuple[str, int, Optional[str], Optional[str]]


class SSHConnectionPool:
    """Process-wide (per VigilEngine instance), keyed by (host, port, username,
    key_path). One shared SSHConnection per physical target regardless of how
    many plugins point at it. Construction is synchronous — SSHConnection's
    __init__ performs no I/O, only its execute*() methods lazily connect."""

    def __init__(self):
        self._conns: Dict[_PoolKey, SSHConnection] = {}

    def get(self, config: PluginConfig) -> SSHConnection:
        ssh_cfg = config.get('ssh_config', {})
        host = ssh_cfg.get('host', config.get('target_host', 'localhost'))
        key: _PoolKey = (
            host,
            ssh_cfg.get('port') or 22,
            ssh_cfg.get('username'),
            ssh_cfg.get('key_path'),
        )
        conn = self._conns.get(key)
        if conn is None:
            conn = SSHConnection.from_config(config)
            self._conns[key] = conn
        return conn

    def close_all(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()


class NetworkOrchestrator:
    """Owns all SSH IO on behalf of a plugin. Plugins never await anything
    themselves — they declare Commands/ActionPlans and this orchestrator
    (driven by VigilEngine) executes them. Long-running jobs are ordinary
    commands too: launched detached on the target and advanced by polling
    commands, so there is no job-specific runtime here (see
    connectors/ssh/job_controller.py and the poll folded into borg's
    requests()/parse_results())."""

    def __init__(self, config: PluginConfig, db: Any, plugin_id: str, target_hint: str,
                 timeout: float, pool: SSHConnectionPool):
        self.ssh_conn = pool.get(config)
        self.target = getattr(self.ssh_conn, 'host', target_hint)
        self._collector = SSHCollector(self.ssh_conn, timeout=timeout)
        self._controller = SSHController(self.ssh_conn)

    async def run(self, commands: List[Command]) -> List[CmdResult]:
        async def _run_one(cmd: Command) -> CmdResult:
            fn = self._controller.execute_action if cmd.action else self._collector.fetch_output
            if cmd.timeout is not None:
                ret, out, err = await fn(cmd.text, timeout=cmd.timeout)
            else:
                ret, out, err = await fn(cmd.text)
            return CmdResult(ret, out, err)

        return list(await asyncio.gather(*(_run_one(c) for c in commands)))

    async def execute(self, plan: ActionPlan) -> CmdResult:
        if plan.timeout is not None:
            ret, out, err = await self._controller.execute_action(plan.command, timeout=plan.timeout)
        else:
            ret, out, err = await self._controller.execute_action(plan.command)
        return CmdResult(ret, out, err)

    async def execute_raw(self, command: str, timeout: Optional[float] = None) -> CmdResult:
        """Escape hatch for one-off ad hoc commands (e.g. web-side dialogs)
        that aren't modeled as a named action. Prefer plan_action()/execute()
        for anything reachable from action_id dispatch."""
        return await self.execute(ActionPlan(command, timeout=timeout))
