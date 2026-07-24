from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Union

from vigil.core.common.plugin_config import PluginConfigMixin
from vigil.core.common.time_utils import parse_duration
from vigil.collector.collectors.ssh_collector import TIMEOUT as SSH_TIMEOUT
from vigil.collector.orchestration.local_io_orchestrator import LocalIOOrchestrator
from vigil.collector.orchestration.network_orchestrator import NetworkOrchestrator, SSHConnectionPool
from vigil.collector.orchestration.storage_orchestrator import StorageOrchestrator
from vigil.collector.orchestration.types import (
    ActionPlan, CmdResult, Command, CollectResult, JobPlan, LocalActionPlan,
)


class CollectorPlugin(PluginConfigMixin, ABC):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: SSHConnectionPool):
        self._init_config(name, config)
        self.db = db
        self.timeout = parse_duration(config.get('timeout', SSH_TIMEOUT))

        self.network = NetworkOrchestrator(config, db, self.id, self.target, self.timeout, ssh_pool)
        self.target = self.network.target
        self.storage = StorageOrchestrator(db, self.target, self.name, self.id)
        self.local_io = LocalIOOrchestrator()

    @abstractmethod
    def commands(self) -> List[Command]:
        """Declare what to run this cycle. Pure — no IO, no side effects.
        Plugins that don't talk over SSH at all (local DNS/HTTP queries,
        etc.) return [] here and implement local_call()/parse_local()
        instead — see those methods below."""

    @abstractmethod
    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Pure: command results in, a CollectResult describing what to
        persist out. No IO, no async, no self.storage/self.network calls."""

    def local_call(self) -> Optional[Callable[[], Any]]:
        """Optional: return a pure zero-arg closure to run off-thread via
        LocalIOOrchestrator for plugins whose collection is local blocking
        IO (DNS resolution, outbound HTTP) rather than an SSH command. The
        closure itself may block/do IO — only constructing it must be pure.
        Return None (the default) for SSH-command-driven plugins."""
        return None

    def parse_local(self, result: Any) -> CollectResult:
        """Pure: consumes local_call()'s return value (or a raised
        exception, passed through as-is if local_call's closure lets one
        escape — plugins should catch what they need to distinguish inside
        the closure and encode it in the return value instead). Only
        implemented by plugins that override local_call()."""
        raise NotImplementedError

    def get_actions(self) -> List[Dict[str, str]]:
        return []

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, JobPlan, LocalActionPlan, CollectResult]]:
        """Pure: decide what command an action requires. Return an
        ActionPlan/JobPlan to run an SSH command, a LocalActionPlan to run
        local blocking IO (no SSH), a CollectResult to apply a write with
        no command run (e.g. logging a refused action), or None for a truly
        unhandled action_id."""
        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs) -> Union[bool, CollectResult]:
        """Pure: given the SSH command's result, return success/failure, or
        a CollectResult (with .success set) to also apply a write, e.g.
        logging a failure message alongside the outcome."""
        return result.exit_code == 0

    def interpret_local_action(self, action_id: str, result: Any, **kwargs) -> Union[bool, CollectResult]:
        """Pure: given a LocalActionPlan's call() return value, return
        success/failure, or a CollectResult (with .success set)."""
        return bool(result)

    def job_on_line(self, action_id: str, **kwargs):
        """Optional streaming line handler for JobPlan actions (e.g. live
        backup progress). Not pure — mirrors JobController's own internal
        buffering, which already writes as output streams in. Return None
        (the default) if the action has no per-line handling."""
        return None

    def interpret_job(self, action_id: str, exit_code: int, **kwargs) -> Union[bool, CollectResult]:
        """Pure: given a JobPlan's exit code, return success/failure, or a
        CollectResult (with .success set). Default: exit_code == 0."""
        return exit_code == 0

    def present(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "actions": self.get_actions()
        }
