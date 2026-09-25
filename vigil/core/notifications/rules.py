"""When a monitor's status is worth a notification.

A rule says which statuses count as a problem for one monitor, how many
problem cycles in a row it takes to notify, whether to repeat while the
problem lasts, and whether to announce the recovery. The tracker applies
those rules to the stream of status writes, one per collection cycle.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
import logging

from vigil.core.connectors.types import Status
from vigil.plugins.base.plugin_helpers import parse_duration

PROBLEM_STATUSES = (Status.FAILED, Status.WARNING, Status.UNAVAILABLE)

PROBLEM = "problem"
REMINDER = "reminder"
RECOVERED = "recovered"


@dataclass(frozen=True)
class Rule:
    """How one monitor notifies."""
    channels: Tuple[str, ...]
    on: FrozenSet[str] = frozenset({Status.FAILED.value})
    after: int = 1
    repeat: int = 0
    recovery: bool = True


def _status(value: str) -> Status:
    """A stored status string as a Status, reading anything unknown as unavailable."""
    try:
        return Status(value)
    except ValueError:
        return Status.UNAVAILABLE


def _merge(base: Dict[str, Any], override: Any) -> Dict[str, Any]:
    """A monitor's `notify:` value layered over the settings it inherits."""
    if override is None or override is True:
        return base
    if override is False:
        return {**base, 'enabled': False}
    if isinstance(override, dict):
        return {**base, 'enabled': True, **override}
    logging.error(f"notify: expected true, false or a mapping, got {override!r}; ignoring it")
    return base


def _rule(settings: Dict[str, Any], channel_ids: List[str], where: str) -> Optional[Rule]:
    """Build a Rule from merged settings, or None when notifications are off."""
    if settings.get('enabled') is False:
        return None
    channels = settings.get('channels', channel_ids)
    if isinstance(channels, str):
        channels = [channels]
    known = [c for c in channels if c in channel_ids]
    for unknown in set(channels) - set(known):
        logging.error(f"{where}: notification channel {unknown!r} is not defined")
    if not known:
        return None

    on = settings.get('on', [Status.FAILED.value])
    if isinstance(on, str):
        on = [on]
    statuses = frozenset(str(s) for s in on if str(s) in {p.value for p in PROBLEM_STATUSES})
    for bad in set(map(str, on)) - statuses:
        logging.error(f"{where}: notify `on` value {bad!r} is not one of failed, warning, unavailable")

    try:
        after = max(1, int(settings.get('after', 1)))
        repeat = parse_duration(settings.get('repeat', 0) or 0)
    except ValueError as e:
        logging.error(f"{where}: bad notify setting ({e}); notifications for it are off")
        return None
    return Rule(tuple(known), statuses, after, repeat, bool(settings.get('recovery', True)))


def resolve_rules(roots: Iterable[Any], defaults: Mapping[str, Any],
                  channel_ids: List[str]) -> Dict[str, Rule]:
    """Every monitor's rule, keyed by monitor id.

    A group's `notify:` settings pass down to everything beneath it. A group
    never notifies for itself, because its status only repeats its children's.
    """
    rules: Dict[str, Rule] = {}

    def walk(plugins: Iterable[Any], inherited: Dict[str, Any]) -> None:
        for plugin in plugins:
            config = getattr(plugin, 'config', None) or {}
            settings = _merge(inherited, config.get('notify'))
            is_group = bool(plugin.children) or config.get('type') == 'group'
            if not is_group:
                rule = _rule(settings, channel_ids, f"monitor {plugin.id!r}")
                if rule is not None:
                    rules[plugin.id] = rule
            walk(plugin.children, settings)

    walk(roots, dict(defaults))
    return rules


@dataclass(frozen=True)
class Alert:
    """One notification the tracker decided to send."""
    kind: str
    status: str
    since: float


@dataclass
class _State:
    alerting: bool = False
    status: Status = Status.ONLINE
    problem_cycles: int = 0
    since: float = 0.0
    last_sent: float = 0.0


@dataclass
class Tracker:
    """Turns each monitor's status writes into the notifications its rule asks for."""
    rules: Dict[str, Rule]
    _states: Dict[str, _State] = field(default_factory=dict)

    def seed(self, plugin_id: str, status: str, since: float, now: float) -> None:
        """Start from a status Vigil held before it restarted, so it is not announced again."""
        rule = self.rules.get(plugin_id)
        if rule is None:
            return
        state = self._states.setdefault(plugin_id, _State())
        if status in rule.on:
            state.alerting, state.status = True, _status(status)
            state.problem_cycles, state.since, state.last_sent = rule.after, since, now

    def alerting(self, plugin_id: str) -> bool:
        """Whether the monitor currently has a problem its rule counts."""
        state = self._states.get(plugin_id)
        return bool(state and state.alerting)

    def observe(self, plugin_id: str, status: str, now: float) -> Optional[Alert]:
        """Record one status write; return the notification it triggers, if any."""
        rule = self.rules.get(plugin_id)
        if rule is None:
            return None
        state = self._states.setdefault(plugin_id, _State())
        current = _status(status)

        if current.value not in rule.on:
            state.problem_cycles = 0
            if not state.alerting:
                return None
            state.alerting, state.status = False, current
            return Alert(RECOVERED, current.value, state.since) if rule.recovery else None

        state.problem_cycles += 1
        if state.problem_cycles == 1:
            state.since = now
        if not state.alerting:
            if state.problem_cycles < rule.after:
                return None
            state.alerting, state.status, state.last_sent = True, current, now
            return Alert(PROBLEM, current.value, state.since)
        if current.severity > state.status.severity:
            state.status, state.last_sent = current, now
            return Alert(PROBLEM, current.value, state.since)
        state.status = current
        if rule.repeat and now - state.last_sent >= rule.repeat:
            state.last_sent = now
            return Alert(REMINDER, current.value, state.since)
        return None
