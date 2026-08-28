"""A push (heartbeat) monitor for targets Vigil cannot reach itself: the
target reports in through the REST API and the plugin never polls —
commands() is empty, and each cycle only re-evaluates the staleness of the
last heartbeat recorded via record_push(). Config: max_age (defaults to twice
the interval), token (authenticates the pushing client), target_host for
display. A fresh heartbeat that reported 'up' is online; no heartbeat yet,
one older than max_age, or a last-reported 'down' is failed."""

import time
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import format_age, format_duration

_DEFAULT_LAYOUT = [
    ['status_card', 'lastbeat_card', 'maxage_card'],
    ['events'],
]

_VALID_PUSH_STATUSES = {'up', 'down'}


class Push(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.max_age = int(config.get('max_age', self.interval * 2))
        self.token = config.get('token')
        self.target = config.get('target_host', self.name)

    def commands(self) -> List[Command]:
        # Purely push-driven: nothing to poll over SSH each cycle. The cycle
        # still runs (via parse([])) so we can evaluate heartbeat staleness.
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        last = self.data.latest_metric('last_push_epoch')

        if last is None:
            return CollectResult(logs=[("No heartbeat received yet", "WARNING")], status='failed')

        age = time.time() - last.value
        if age > self.max_age:
            return CollectResult(
                logs=[(
                    f"No heartbeat for {format_age(int(age))}, exceeds max_age of "
                    f"{format_duration(self.max_age)}",
                    "ERROR",
                )],
                status='failed',
            )

        last_reported = self.data.latest_metric('reported_up')
        if last_reported is not None and last_reported.value == 0.0:
            return CollectResult(status='failed')
        return CollectResult(status='online')

    def record_push(self, status: str = 'up', msg: Optional[str] = None,
                    value: Optional[float] = None) -> bool:
        if status not in _VALID_PUSH_STATUSES:
            return False

        now = time.time()
        is_up = status == 'up'
        metrics = {'last_push_epoch': now, 'reported_up': 1.0 if is_up else 0.0}
        if value is not None:
            metrics['value'] = float(value)

        log_level = "INFO" if is_up else "ERROR"
        detail = f": {msg}" if msg else ""
        result = CollectResult(
            metrics=metrics,
            logs=[(f"Heartbeat received (status={status}){detail}", log_level)],
            status='online' if is_up else 'failed',
        )
        self.engine.apply(self, result)
        return True

    @property
    def _last_heartbeat_text(self) -> str:
        last = self.data.latest_metric('last_push_epoch')
        if last is None:
            return 'Never'
        return format_age(int(time.time() - last.value))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'status_card': {'metric': 'reported_up', 'title': 'LAST REPORTED STATUS',
                                'on_text': 'UP', 'off_text': 'DOWN'},
                'lastbeat_card': {'title': 'LAST HEARTBEAT', 'value_attr': '_last_heartbeat_text',
                                  'refresh': True},
                'maxage_card': {'title': 'MAX AGE', 'value': format_duration(self.max_age)},
            },
            'events': True,
        }

