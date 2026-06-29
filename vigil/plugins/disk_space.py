from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart
from vigil.core.ui.theme import STATUS_COLORS


def _format_gb(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{gb * 1024:.0f} MB"


_DEFAULT_LAYOUT = {
    'grid_columns': 3,
    'widgets': {
        'host_card':      {'col_span': 1},
        'path_card':      {'col_span': 1},
        'threshold_card': {'col_span': 1},
        'usage_card':     {'col_span': 1},
        'avail_card':     {'col_span': 1},
        'total_card':     {'col_span': 1},
        'chart':          {'col_span': 3},
        'logs':           {'col_span': 3},
    }
}


class DiskSpacePlugin(BasePlugin):
    """
    Monitors disk space usage for a path or mountpoint over SSH via `df`.
    Works on any mounted filesystem — no ZFS or other tools required.
    Reports failed when usage exceeds the configured threshold.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.path = config.get('path', '/')
        self.threshold = int(config.get('threshold', 90))
        self.ssh_collector = self.internal_modules['collectors']['ssh']
        self.db_logger = self.internal_modules['loggers']['db_logs']
        self.db_metrics = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        # Single-quoted path prevents shell expansion; --output avoids line-wrap issues
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            f"df --output=size,used,avail,pcent -B1 '{self.path}' | tail -1"
        )
        if ret != 0:
            self.db_logger.write(f"df failed for '{self.path}': {stderr}", level="ERROR")
            self.set_status('failed')
            return

        try:
            fields = stdout.strip().split()
            size_bytes = int(fields[0])
            used_bytes = int(fields[1])
            avail_bytes = int(fields[2])
            used_pct = float(fields[3].rstrip('%'))
        except (IndexError, ValueError) as e:
            self.db_logger.write(f"Failed to parse df output '{stdout.strip()}': {e}", level="ERROR")
            self.set_status('failed')
            return

        size_gb  = size_bytes  / (1024 ** 3)
        used_gb  = used_bytes  / (1024 ** 3)
        avail_gb = avail_bytes / (1024 ** 3)

        self.db_metrics.metric('used_pct',  used_pct)
        self.db_metrics.metric('size_gb',   size_gb)
        self.db_metrics.metric('used_gb',   used_gb)
        self.db_metrics.metric('avail_gb',  avail_gb)

        level = 'WARNING' if used_pct >= self.threshold else 'INFO'
        self.db_logger.write(
            f"{self.path}: {used_pct:.1f}% used "
            f"({_format_gb(used_gb)} of {_format_gb(size_gb)}, "
            f"{_format_gb(avail_gb)} free, threshold {self.threshold}%)",
            level=level
        )
        self.set_status('failed' if used_pct >= self.threshold else 'online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.layout import PluginLayout

        def latest(metric_name):
            return Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).first()

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT)

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('path_card'):
            info_card('PATH', self.path)
        with layout.cell('threshold_card'):
            info_card('THRESHOLD', f'{self.threshold}%')
        with layout.cell('usage_card'):
            usage_label = info_card('USAGE', '-- %')
        with layout.cell('avail_card'):
            avail_label = info_card('AVAILABLE', '--')
        with layout.cell('total_card'):
            total_label = info_card('TOTAL SIZE', '--')
        with layout.cell('chart'):
            history_chart(f'USAGE HISTORY — {self.path} (%)', self.name, 'used_pct')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            pct   = latest('used_pct')
            avail = latest('avail_gb')
            total = latest('size_gb')
            if pct:
                usage_label.text = f'{pct.value:.1f}%'
                color = STATUS_COLORS['failed'] if pct.value >= self.threshold else STATUS_COLORS['online']
                usage_label.style(f'color: {color}')
            if avail:
                avail_label.text = _format_gb(avail.value)
            if total:
                total_label.text = _format_gb(total.value)

        ui.timer(5.0, update_cards)
