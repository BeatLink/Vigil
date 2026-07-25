from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from vigil.core.coordination.data_view import PluginDataView
    from vigil.core.connectors.types import Request, Result

from vigil.plugins.base.plugin_helpers import PluginConfigMixin, parse_duration
from vigil.core.connectors.ssh.ssh import COLLECT_TIMEOUT as SSH_TIMEOUT
from vigil.core.contracts import ActionButtonSpec, EngineLike
from vigil.core.settings.config_schema import PluginConfig
from vigil.core.connectors.types import (
    ActionOutcome, ActionPlanResult, CmdResult, Command, CollectResult,
)
from vigil.core.ui.spec_types import UISpec


class Plugin(PluginConfigMixin, ABC):
    engine: Optional[EngineLike] = None

    # Read-only projection of the Database Engine, injected in __init__. The
    # only handle a pure plugin holds into stored state (reads only — no IO,
    # no writes). See vigil/core/coordination/data_view.py.
    data: "PluginDataView"

    # Plugins that use the declarative render path (spec.generic_render)
    # override this as a @property returning a UISpec dict. Plugins with a
    # hand-written render_ui() may leave it unset — every UI_SPEC consumer
    # (spec._dialog_spec_for, main_dashboard.py) already treats a missing
    # UI_SPEC as "no declarative UI", via getattr(plugin, 'UI_SPEC', None).
    UI_SPEC: Optional[UISpec] = None

    def __init__(self, name: str, config: PluginConfig):
        """Pure plugins take only their name and config. All IO/persistence
        machinery (SSH/HTTP connectors, the storage writer, the read view) is
        owned by the Coordination Engine and wired to the plugin after
        construction via bind() — see VigilEngine.setup_modules."""
        self._init_config(name, config)
        self.timeout = parse_duration(config.get('timeout', SSH_TIMEOUT))
        self.data = None  # PluginDataView, injected by engine.bind()

    def bind(self, data: "PluginDataView") -> None:
        """Called once by the Coordination Engine to hand the plugin its
        read-only Database Engine projection. The engine keeps the write path
        (StorageOrchestrator) and connectors keyed by plugin id on its side."""
        self.data = data

    @property
    def ui(self):
        """UI-construction helper (host/status cards, tables) for the handful
        of plugins with a bespoke render_ui(). Built lazily so a plugin that
        never renders — or is collected headless in a test — pays nothing."""
        cached = self.__dict__.get('_ui')
        if cached is None:
            from vigil.core.ui.orchestration import UIOrchestrator
            cached = self.__dict__['_ui'] = UIOrchestrator(self)
        return cached

    def requests(self) -> List["Request"]:
        """Declare this cycle's IO as a heterogeneous list of connector
        requests (Command / HttpRequest / DnsQuery / PingRequest). Pure — no
        IO, no side effects. The Connector Engine executes them and hands the
        positionally-matched results to parse_results().

        Default: delegate to the SSH-only commands() so existing plugins that
        only override commands()/parse() need no change."""
        return self.commands()

    def parse_results(self, results: List["Result"]) -> CollectResult:
        """Pure: connector results in, a CollectResult describing what to
        persist out. No IO, no async, no self.data/self.storage calls.

        Default: delegate to parse() (whose results are the Command-only
        case), so SSH-only plugins need no change."""
        return self.parse(results)

    @abstractmethod
    def commands(self) -> List[Command]:
        """Declare what SSH commands to run this cycle. Pure — no IO, no side
        effects. Plugins that talk over HTTP/DNS/ICMP instead override
        requests()/parse_results() with the declarative request types and
        return [] here."""

    @abstractmethod
    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Pure: SSH command results in, a CollectResult describing what to
        persist out. No IO, no async, no self.storage/self.network calls."""

    def io_call(self) -> Optional[Callable[[], Any]]:
        """Escape hatch for the rare plugin whose collection is genuinely
        sequential/conditional local IO that the declarative requests() list
        can't express (e.g. ddns_updater: fetch public IP, resolve DNS,
        compare, then conditionally push). Return a zero-arg closure (sync or
        async) the Coordination Engine runs off the event loop; its return
        value is handed to parse_results() as the single result.

        Constructing the closure must be pure; the closure itself may block/do
        IO. Default None: use requests()/parse_results() (the 99% path)."""
        return None

    def get_actions(self) -> List[ActionButtonSpec]:
        return []

    def plan_action(self, action_id: str, **kwargs) -> ActionPlanResult:
        """Pure: decide what an action requires. Return an ActionPlan for an
        SSH command (including launching a detached long-running job — see
        borg), a declarative connector request (HttpRequest/DnsQuery/
        PingRequest), an IoActionPlan for sequential local IO, a CollectResult
        to apply a write with no IO (e.g. logging a refused action), or None
        for an unhandled action_id."""
        return None

    def interpret_action(self, action_id: str, result: Any, **kwargs) -> ActionOutcome:
        """Pure: given the action's result (a CmdResult for SSH, an
        HttpResult/DnsResult/PingResult for a connector request, or an
        IoActionPlan closure's return value), return success/failure, or a
        CollectResult (with .success set) to also apply a write, e.g. logging
        a failure message alongside the outcome. Default assumes a CmdResult."""
        return result.exit_code == 0

    async def on_action(self, action_id: str, **kwargs) -> bool:
        success, _ = await self.engine.dispatch_action(self, action_id, **kwargs)
        return success

    async def action_with_output(self, action_id: str, **kwargs) -> tuple:
        success, metadata = await self.engine.dispatch_action(self, action_id, **kwargs)
        return success, str((metadata or {}).get('content') or '')

    async def run_cycle(self) -> bool:
        return await self.engine.run_cycle_now(self)

    async def present(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "actions": self.get_actions(),
        }

    @abstractmethod
    def render_ui(self, context: str = 'page'):
        pass
