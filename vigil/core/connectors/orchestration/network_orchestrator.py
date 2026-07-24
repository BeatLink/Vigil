import asyncio
from typing import Any, Dict, List, Optional, Tuple

from vigil.core.connectors.ssh_connector import SSHConnection
from vigil.core.connectors.ssh_runner import SSHCollector, SSHController
from vigil.core.connectors.job_controller import JobController
from vigil.core.connectors.orchestration.types import ActionPlan, CmdResult, Command, JobPlan

_PoolKey = Tuple[str, int, Optional[str], Optional[str]]


class SSHConnectionPool:
    """Process-wide (per VigilEngine instance), keyed by (host, port, username,
    key_path). One shared SSHConnection per physical target regardless of how
    many plugins point at it. Construction is synchronous — SSHConnection's
    __init__ performs no I/O, only its execute*() methods lazily connect."""

    def __init__(self):
        self._conns: Dict[_PoolKey, SSHConnection] = {}

    def get(self, config: Dict[str, Any]) -> SSHConnection:
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
    """Owns all SSH/subprocess IO on behalf of a plugin. Plugins never await
    anything themselves — they declare Commands/ActionPlans/JobPlans and this
    orchestrator (driven by VigilEngine) executes them."""

    def __init__(self, config: Dict[str, Any], db: Any, plugin_id: str, target_hint: str,
                 timeout: float, pool: SSHConnectionPool):
        self.ssh_conn = pool.get(config)
        self.target = getattr(self.ssh_conn, 'host', target_hint)
        self._collector = SSHCollector(self.ssh_conn, timeout=timeout)
        self._controller = SSHController(self.ssh_conn)
        self._job = JobController(self.ssh_conn, db, plugin_id, self.target)

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

    async def run_job_plan(self, plan: JobPlan, on_line=None) -> Tuple[int, int]:
        return await self._job.run_job(
            plan.kind, plan.command, redacted=plan.redacted, on_line=on_line, timeout=plan.timeout,
        )

    def is_running(self) -> bool:
        return self._job.is_running()

    def current_job_id(self) -> Optional[int]:
        return self._job.current_job_id()

    def cancel(self) -> bool:
        return self._job.cancel()

    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        return self._job.recent(limit=limit, kind=kind)

    def output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        return self._job.output(job_id, after_seq=after_seq, limit=limit)
