import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart


_DEFAULT_LAYOUT = {
    'grid_columns': 4,
    'widgets': {
        'host_card':      {'col_span': 1},
        'pool_card':      {'col_span': 1},
        'usage_card':     {'col_span': 1},
        'threshold_card': {'col_span': 1},
        'chart':          {'col_span': 4},
        'logs':           {'col_span': 4},
    }
}


class ZFSPoolPlugin(BasePlugin):
    """
    Monitors ZFS zpool capacity over SSH.
    Reports usage percentage and marks the pool failed when it exceeds the threshold.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.pool = config.get('pool')
        self.threshold = int(config.get('threshold', 90))
        self.ssh_collector = self.internal_modules['collectors'].get('ssh')
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            f"zpool list -H -o name,capacity {self.pool}"
        )

        if ret != 0:
            self.db_logger.write(f"zpool list failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        try:
            # Output format: "<pool>\t<capacity>%"
            usage_pct = float(stdout.strip().split()[1].rstrip('%'))
        except (IndexError, ValueError) as e:
            self.db_logger.write(f"Failed to parse zpool output '{stdout.strip()}': {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric("usage_pct", usage_pct)
        level = "WARNING" if usage_pct >= self.threshold else "INFO"
        self.db_logger.write(
            f"Pool {self.pool}: {usage_pct:.1f}% used (threshold {self.threshold}%)",
            level=level
        )
        self.set_status('failed' if usage_pct >= self.threshold else 'online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.theme import STATUS_COLORS
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('pool_card'):
            info_card('POOL', self.pool)
        with layout.cell('usage_card'):
            usage_label = info_card('USAGE', '-- %')
        with layout.cell('threshold_card'):
            info_card('THRESHOLD', f'{self.threshold}%')
        with layout.cell('chart'):
            history_chart(f'CAPACITY HISTORY — {self.pool} (%)', self.name, 'usage_pct')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_usage():
            last = Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == 'usage_pct')
            ).order_by(Metric.timestamp.desc()).first()
            if last:
                pct = last.value
                usage_label.text = f'{pct:.1f}%'
                color = STATUS_COLORS['failed'] if pct >= self.threshold else STATUS_COLORS['online']
                usage_label.style(f'color: {color}')

        ui.timer(5.0, update_usage)
