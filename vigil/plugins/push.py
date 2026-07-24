import time
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.time_utils import format_age, format_duration

_DEFAULT_LAYOUT = [
    ['status_card', 'lastbeat_card', 'maxage_card'],
    ['events'],
]

_VALID_PUSH_STATUSES = {'up', 'down'}


class PushCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.max_age = int(config.get('max_age', self.interval * 2))
        self.token = config.get('token')
        self.target = config.get('target_host', self.name)


    async def on_collect(self):
        last = self.latest_metric('last_push_epoch')

        if last is None:
            self.db_logger.write("No heartbeat received yet", level="WARNING")
            self.set_status('failed')
            return

        age = time.time() - last.value
        if age > self.max_age:
            self.db_logger.write(
                f"No heartbeat for {format_age(int(age))}, exceeds max_age of "
                f"{format_duration(self.max_age)}",
                level="ERROR"
            )
            self.set_status('failed')
            return

        last_reported = self.latest_metric('reported_up')
        if last_reported is not None and last_reported.value == 0.0:
            self.set_status('failed')
        else:
            self.set_status('online')

    def record_push(self, status: str = 'up', msg: Optional[str] = None,
                    value: Optional[float] = None) -> bool:
        if status not in _VALID_PUSH_STATUSES:
            return False

        now = time.time()
        is_up = status == 'up'
        self.db_metrics.metric('last_push_epoch', now)
        self.db_metrics.metric('reported_up', 1.0 if is_up else 0.0)
        if value is not None:
            self.db_metrics.metric('value', float(value))

        log_level = "INFO" if is_up else "ERROR"
        detail = f": {msg}" if msg else ""
        self.db_logger.write(f"Heartbeat received (status={status}){detail}", level=log_level)
        self.set_status('online' if is_up else 'failed')
        return True

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class PushUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        max_age = int(self.config.get('max_age', self.interval * 2))

        page = self.page()

        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
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
            self.internal_modules['ui']['events_table'](page)

        def update():
            last = self.latest_metric('last_push_epoch')
            if last is not None:
                age = int(time.time() - last.value)
                lastbeat_label.text = format_age(age)

        page.on_refresh(update)
        update()
        page.start()
