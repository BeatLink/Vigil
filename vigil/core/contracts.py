"""Shared interface contracts used across module boundaries.

Types that belong to a single subsystem live with that subsystem (e.g.
CollectResult in connectors/types.py). This module holds only the
contracts that cross subsystem boundaries and would otherwise be
duck-typed identically in multiple places — see DEVELOP.md's "Interface
contracts" section for the rationale behind each one.
"""

from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Protocol, TypedDict, Union,
    runtime_checkable,
)

# A callback that may be a plain sync function or one returning an
# awaitable — both are valid everywhere NiceGUI/DataBus invoke a callback,
# since `helpers.should_await` (or an equivalent check) decides at the call
# site whether to await the result. Previously reimplemented ad hoc in
# DataBus.emit, PluginPage._tick, safe_timer, and on_data_event.
RefreshCallback = Callable[[], Union[None, Awaitable[None]]]

# The five DataBus event kinds. Not a Literal, deliberately: DataBus.emit is
# instructed to broadcast the exact string it's given, and adding a new
# writer that emits a different kind should not require touching this file.
# Callers that want closed-set safety can still write `event: EventName`
# themselves; this exists so both emit and subscribe sites reference one
# shared name instead of typing the string literal independently.
EventName = str


@runtime_checkable
class MetricsSource(Protocol):
    """The narrow, read-only slice of DatabaseManager that anything
    reporting metrics externally (Prometheus/InfluxDB exporters, the
    public REST API) actually needs. Neither exporter should require the
    rest of DatabaseManager's write/job/setting surface."""

    def latest_metrics(self) -> List[Dict[str, Any]]: ...

    def latest_statuses(self) -> Dict[str, str]: ...


@runtime_checkable
class PushablePlugin(Protocol):
    """The extension surface a push-monitor plugin exposes beyond the base
    Plugin ABC. Only vigil.plugins.push.Push implements this today;
    core/ui/api.py's /api/push route narrows a generic Plugin to this
    Protocol (via isinstance, since Protocol supports runtime_checkable
    structural checks) instead of importing the concrete Push class."""

    token: Optional[str]

    def record_push(self, status: str, msg: Optional[str] = None, value: Optional[float] = None) -> bool: ...


class ActionButtonSpec(TypedDict, total=False):
    """One entry in Plugin.get_actions()'s return list — the generic
    "custom action button" system rendered by main_dashboard.py's plugin
    detail header. Distinct from spec.py's ButtonSpec (a UI_SPEC-embedded
    button rendered inline in the page body via render_buttons); this one
    is always rendered in the fixed header regardless of UI_SPEC."""
    action_id: str
    name: str
    icon: str
    variant: str


@runtime_checkable
class EngineLike(Protocol):
    """The surface of VigilEngine that the UI layer (main_dashboard.py,
    api.py) and Plugin.on_action/action_with_output/run_cycle actually call.
    Exists so those modules can be typed against a narrow contract instead
    of the concrete VigilEngine, and so a test double only needs to satisfy
    this shape rather than VigilEngine's full constructor/internals."""

    @property
    def db(self) -> Any: ...

    @property
    def plugins(self) -> List[Any]: ...

    @property
    def config_loader(self) -> Any: ...

    async def run(self) -> None: ...

    async def dispatch_action(self, plugin: Any, action_id: str, **kwargs) -> tuple: ...

    async def run_cycle_now(self, plugin: Any) -> bool: ...

    # Job-control surface the UI Engine's job panel drives through the engine
    # (these touch the live JobController, which a pure plugin no longer holds).
    def job_is_running(self, plugin: Any) -> bool: ...

    def job_current_id(self, plugin: Any) -> Optional[int]: ...

    def job_recent(self, plugin: Any, limit: int = 20) -> list: ...

    async def job_cancel(self, plugin: Any) -> bool: ...
