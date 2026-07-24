import time
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.time_utils import format_age, format_duration

_DEFAULT_LAYOUT = [
    ['status_card', 'lastbeat_card', 'maxage_card'],
    ['events'],
]

_VALID_PUSH_STATUSES = {'up', 'down'}


class PushCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.max_age = int(config.get('max_age', self.interval * 2))
        self.token = config.get('token')
        self.target = config.get('target_host', self.name)

    def commands(self) -> List[Command]:
        # Purely push-driven: nothing to poll over SSH each cycle. The cycle
        # still runs (via parse([])) so we can evaluate heartbeat staleness.
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        last = self.storage.latest_metric('last_push_epoch')

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

        last_reported = self.storage.latest_metric('reported_up')
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
        self.storage.apply(result)
        return True


class PushUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        max_age = int(self.config.get('max_age', self.interval * 2))

        page = self.ui.page()

        with layout.cell('status_card'):
            self.ui.status_card(
                page,
                metric_name='reported_up',
                title='LAST REPORTED STATUS',
                on_text='UP',
                off_text='DOWN'
            )
        with layout.cell('lastbeat_card'):
            lastbeat_label = info_card('LAST HEARTBEAT', 'Never')
        with layout.cell('maxage_card'):
            info_card('MAX AGE', format_duration(max_age))
        with layout.cell('events'):
            self.ui.events_table(page)

        def update():
            last = self.storage.latest_metric('last_push_epoch')
            if last is not None:
                age = int(time.time() - last.value)
                lastbeat_label.text = format_age(age)

        page.on_refresh(update)
        update()
        page.start()
