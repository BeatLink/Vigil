"""The Plugin contract every monitor derives from. A plugin is pure: it
declares its IO as data (requests()/commands()), parses connector results into
a CollectResult (parse_results()/parse()), plans and interprets actions, and
describes its UI via UI_SPEC, while the Coordination Engine owns every
connector, write path, and side effect. Agent-backed targets extend the same
contract with push: subscriptions() declares event streams, and SAMPLED lets
the agent run a single-command plugin locally at the monitor's interval and
push results, suppressing unchanged ones with a five-interval keepalive."""

from abc import ABC
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from vigil.core.coordination.data_view import PluginDataView
    from vigil.core.connectors.types import Request, Result
    from vigil.core.connectors.agent_protocol import StreamSpec

from vigil.plugins.base.plugin_helpers import PluginConfigMixin, parse_duration
from vigil.core.connectors.ssh_connector import COLLECT_TIMEOUT as SSH_TIMEOUT
from vigil.core.contracts import ActionButtonSpec, EngineLike
from vigil.core.settings.config_schema import PluginConfig
from vigil.core.connectors.types import (
    ActionOutcome, ActionPlanResult, CmdResult, Command, CollectResult,
)
from vigil.core.ui.spec_types import UISpec


class Plugin(PluginConfigMixin, ABC):
    """A monitor. Exposes only pure functions and data: the engine owns all IO
    and persistence. "Pure" constrains effects, not memory — keeping derived
    state between cycles is fine; holding a connection or database handle, or
    doing IO inside requests()/parse_results(), is not."""

    engine: Optional[EngineLike] = None

    # Read-only projection of the Database Engine, injected in __init__. The
    # only handle a pure plugin holds into stored state (reads only — no IO,
    # no writes). See vigil/core/coordination/data_view.py.
    data: "PluginDataView"

    # Detached-job control scoped to this plugin, injected by the engine's
    # wiring. See vigil/core/coordination/jobs.py.
    jobs = None

    # Plugins that use the declarative render path (spec.generic_render)
    # override this as a @property returning a UISpec dict. Plugins with a
    # hand-written render_ui() may leave it unset — every UI_SPEC consumer
    # (spec._dialog_spec_for, main_dashboard.py) already treats a missing
    # UI_SPEC as "no declarative UI", via getattr(plugin, 'UI_SPEC', None).
    UI_SPEC: Optional[UISpec] = None

    # Collect on the agent rather than on the poll: the agent runs commands()
    # locally at this monitor's interval and pushes each result, so a cycle
    # costs no round trip and the engine starts no polling task. Only for
    # single-command plugins cheap enough to run on the target unattended, and
    # never for one whose commands() varies with state — the stream is built
    # once, when it is registered, so borg switching to a job poll would pin
    # the agent to whichever command happened to be current.
    SAMPLED: bool = False

    # Suffix of the generic sample stream's id, distinct so a plugin can carry
    # other streams beside it.
    SAMPLE_STREAM = 'sample'

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
        (db.apply_result) and connectors keyed by plugin id on its side."""
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

    def commands(self) -> List[Command]:
        """Declare what SSH commands to run this cycle. Pure — no IO, no side
        effects. Plugins that talk over HTTP/DNS/ICMP override
        requests()/parse_results() instead and leave this default."""
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Pure: SSH command results in, a CollectResult describing what to
        persist out. No IO, no async, no self.storage/self.network calls."""
        return CollectResult()

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

    def subscriptions(self) -> List["StreamSpec"]:
        """Declare the event streams this monitor wants pushed to it, for
        targets reached by an agent. Pure — no IO.

        Each returned StreamSpec names an agent-side watcher (``kind``) and its
        parameters; the agent watches that source locally and sends a frame the
        instant something changes, so detection latency stops being a function
        of `interval`. The engine ignores these for a monitor that has no
        agent, which is what lets a plugin declare both a poll path and a push
        path and run correctly over either transport.

        Default: when SAMPLED is set and commands() is a single command, the
        generic sample stream — the poll's own command, run by the agent at
        this monitor's own interval and pushed as it completes. `max_quiet`
        lets the agent skip pushing unchanged results while still sending one
        at least every fifth interval, so a quiet-but-healthy monitor can
        never read as stale; an older agent ignores the param. A subclass
        with its own streams composes with super()."""
        if not self.SAMPLED:
            return []
        commands = self.commands()
        if len(commands) != 1:
            return []
        from vigil_agent.protocol import StreamSpec
        return [StreamSpec(
            id=f'{self.id}:{self.SAMPLE_STREAM}',
            kind='sample',
            params={'command': commands[0].text, 'interval': self.interval,
                    'max_quiet': self.interval * 5},
        )]

    def event_driven(self) -> bool:
        """Whether pushed events fully replace this monitor's poll. The engine
        starts no polling loop for a monitor that says yes and has an agent.

        Derived from the stream actually produced, not from SAMPLED alone: a
        plugin whose commands() is empty or multi-command yields no sample
        stream, and suppressing its poll would leave it collecting nothing."""
        return any(s.kind == 'sample' for s in self.subscriptions())

    def parse_event(self, stream_id: str, payload: Dict[str, Any],
                    timestamp: float) -> Optional[CollectResult]:
        """Pure: turn one pushed event into a CollectResult to persist, or
        None to ignore it. Called out of band from the polling cycle, once per
        inbound frame, so it must stay cheap as well as pure.

        `stream_id` identifies which of this plugin's subscriptions fired.
        Default: interpret the generic sample frame through the plugin's own
        parse() — the agent sends back the same (exit_code, stdout, stderr)
        triple the command connector returns, so the poll's parser needs no
        push variant. A subclass with its own streams delegates unrecognized
        stream ids to super()."""
        if not stream_id.endswith(f':{self.SAMPLE_STREAM}'):
            return None
        return self.parse([CmdResult(
            int(payload.get('exit_code', -1)),
            str(payload.get('stdout', '')),
            str(payload.get('stderr', '')),
        )])

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

    async def run_action(self, action_id: str, **kwargs) -> tuple:
        """Dispatch one UI-triggered action through the engine; returns (success, content)."""
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

    def render_ui(self, context: str = 'page'):
        """Render the declarative UI_SPEC; only bespoke plugins override."""
        if getattr(self, 'UI_SPEC', None):
            from vigil.core.ui.spec import generic_render
            generic_render(self, context)
