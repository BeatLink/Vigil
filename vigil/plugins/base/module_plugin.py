"""Shared scaffolding for the modular monitors — system_stats, network, disks.
A modular plugin owns a list of opt-in modules, concatenates their declared
commands into one cycle, slices the results back out positionally, and reports
the worst module status as its own. Each module may run on its own interval.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.plugins.base.plugin_helpers import parse_duration
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.connectors.agent_protocol import StreamSpec

# 'offline' sits below 'warning': a module that cannot measure is less alarming
# than one measuring a bad number.
SEVERITY = {'online': 0, 'offline': 1, 'warning': 2, 'failed': 3}

LOG_LEVEL = {'online': 'INFO', 'warning': 'WARNING', 'offline': 'WARNING', 'failed': 'ERROR'}

# Slack on the due check, so a module whose interval equals the plugin's is not
# pushed onto every second cycle by scheduling jitter.
_DUE_TOLERANCE_SECONDS = 0.5


def worst_status(statuses: List[str]) -> str:
    """The most severe of the given statuses, defaulting to online."""
    return max(statuses, key=lambda s: SEVERITY.get(s, 1)) if statuses else 'online'


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------

class Module:
    """One collected signal. Subclasses stay pure: no IO, no persistence."""

    key = ''

    def __init__(self, plugin: 'ModularPlugin', options: Dict[str, Any]):
        self.plugin = plugin
        self.options = options
        raw_interval = options.get('interval')
        self.interval: Optional[float] = (
            parse_duration(raw_interval) if raw_interval is not None else None)
        self._last_run: Optional[float] = None
        self._last_status: Optional[str] = None

    def due(self, now: float) -> bool:
        """Whether this module collects on the cycle starting at `now`."""
        if self.interval is None or self._last_run is None:
            return True
        return (now - self._last_run) >= (self.interval - _DUE_TOLERANCE_SECONDS)

    def mark_run(self, now: float) -> None:
        """Stamp this cycle as the module's last collection."""
        self._last_run = now

    @property
    def carried_status(self) -> Optional[str]:
        """The status from this module's last collection, held while it is not due."""
        return self._last_status

    def commands(self) -> List[Command]:
        raise NotImplementedError

    def parse(self, results: List[CmdResult]) -> CollectResult:
        raise NotImplementedError

    def subscriptions(self) -> List[StreamSpec]:
        """Agent-pushed event streams this module wants; poll-only by default."""
        return []

    def parse_event(self, payload: Dict[str, Any]) -> Optional[CollectResult]:
        """One pushed event in, a CollectResult out, or None to ignore it."""
        return None

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {}

    def card_row(self) -> List[str]:
        """Cards that join the shared top row; the rest are placed by rows()."""
        return list(self.cards())

    def rows(self) -> List[List[str]]:
        """Full-width layout rows this module adds above its charts."""
        return []


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def module_options(label: str, config: Dict[str, Any],
                   module_types: Sequence[type],
                   defaults: Sequence[str] = ()) -> Dict[str, Dict[str, Any]]:
    """Resolve the `modules` config block to {module key: options}. Omitting the
    block entirely selects `defaults`; naming anything means the config is
    driving, and only what it names is on."""
    known = [t.key for t in module_types]
    raw = config.get('modules')

    if raw is None:
        return {key: {} for key in known if key in defaults}

    if isinstance(raw, list):
        requested = {key: {} for key in raw}
    elif isinstance(raw, dict):
        requested = {}
        for key, options in raw.items():
            if options is False:
                continue
            options = {} if options in (True, None) else dict(options)
            if options.get('enabled', True):
                requested[key] = options
    else:
        raise ValueError(f"{label}: `modules` must be a mapping or a list, got {raw!r}")

    unknown = [key for key in requested if key not in known]
    if unknown:
        raise ValueError(
            f"{label}: unknown module(s) {unknown} — known modules are {known}")

    return {key: requested[key] for key in known if key in requested}


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class ModularPlugin(Plugin):
    """A monitor whose signals are opt-in modules sharing one collection cycle."""

    MODULE_TYPES: Sequence[type] = ()
    MODULE_LABEL = ''

    # The modules a monitor runs when its config names none. Every one must work
    # on an ordinary Linux host with no extra packages, no privileges and no
    # particular hardware, so a default set never reports offline for absence.
    DEFAULT_MODULES: Sequence[str] = ()

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        options = module_options(self.MODULE_LABEL, config, self.MODULE_TYPES,
                                 self.DEFAULT_MODULES)
        self.modules: List[Module] = [
            module_type(self, options[module_type.key])
            for module_type in self.MODULE_TYPES if module_type.key in options
        ]
        self._spans: List[Tuple[Module, int, int]] = []
        self._idle: List[Module] = []

    def _now(self) -> float:
        """The clock the due check runs against; overridden in tests."""
        return time.monotonic()

    def _module(self, key: str) -> Optional[Module]:
        """The enabled module with this key, or None when it is off."""
        return next((m for m in self.modules if m.key == key), None)

    def commands(self) -> List[Command]:
        """Concatenate the due modules' commands, recording the half-open range
        each one occupies so parse() can hand it back exactly its own results."""
        now = self._now()
        commands, spans, idle = [], [], []
        for module in self.modules:
            if not module.due(now):
                idle.append(module)
                continue
            module.mark_run(now)
            start = len(commands)
            commands.extend(module.commands())
            spans.append((module, start, len(commands)))
        self._spans, self._idle = spans, idle
        return commands

    def subscriptions(self) -> List[StreamSpec]:
        return [spec for module in self.modules for spec in module.subscriptions()]

    def parse_event(self, stream_id: str, payload: Dict[str, Any],
                    timestamp: float) -> Optional[CollectResult]:
        for module in self.modules:
            result = module.parse_event(payload)
            if result is not None:
                return result
        return None

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Merge every due module's result, holding the last status of the ones
        that sat this cycle out."""
        if not self.modules:
            return CollectResult.failed(
                "No modules enabled — an empty `modules` block selects nothing. "
                f"Name at least one of {[t.key for t in self.MODULE_TYPES]}, or "
                "omit `modules` for the defaults "
                f"({list(self.DEFAULT_MODULES)}).",
                level="WARNING", status='offline')

        metrics: Dict[str, float] = {}
        settings: Dict[str, str] = {}
        logs: List[Tuple[str, str]] = []
        statuses: List[str] = []

        for module, start, end in self._spans:
            result = module.parse(results[start:end])
            metrics.update(result.metrics)
            settings.update(result.settings)
            logs.extend(result.logs)
            if result.status:
                module._last_status = result.status
                statuses.append(result.status)

        statuses.extend(m.carried_status for m in self._idle if m.carried_status)

        return CollectResult(metrics=metrics, logs=logs, settings=settings,
                             status=worst_status(statuses))

    @property
    def UI_SPEC(self):
        cards, charts, module_rows, card_row = {}, {}, [], ['host_card']
        for module in self.modules:
            cards.update(module.cards())
            charts.update(module.charts())
            card_row.extend(module.card_row())
            module_rows.extend(module.rows() + [[name] for name in module.charts()])
        layout = [card_row] + module_rows + [['events']]
        return {
            'layout': layout,
            'cards': cards,
            'charts': charts,
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
