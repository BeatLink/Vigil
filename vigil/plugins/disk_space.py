from typing import Dict, Any
from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

from vigil.core.common.plugin_utils import format_bytes as _format_gb


_DEFAULT_LAYOUT = [
    ['host_card', 'path_card', 'threshold_card'],
    ['usage_card', 'avail_card', 'total_card'],
    ['chart'],
    ['events'],
]


class DiskSpaceCollectorPlugin(CollectorPlugin):
    """
    Monitors disk space usage for a path or mountpoint over SSH via `df`.
    Works on any mounted filesystem — no ZFS or other tools required.
    Reports failed when usage exceeds the configured threshold.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.path = config.get('path', '/')
        self.threshold = int(config.get('threshold', 90))

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


class DiskSpaceUIPlugin(UIPlugin):
    """Dashboard rendering for the disk_space monitor."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.path = config.get('path', '/')
        self.threshold = int(config.get('threshold', 90))

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['used_pct', 'avail_gb', 'size_gb'])

        def _pct_or_dash(v):
            return '-- %' if v is None else f'{v:.1f}%'

        def _gb_or_dash(v):
            return '--' if v is None else _format_gb(v)

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('path_card'):
            info_card('PATH', self.path)
        with layout.cell('threshold_card'):
            info_card('THRESHOLD', f'{self.threshold}%')
        with layout.cell('usage_card'):
            usage_label = info_card('USAGE', '-- %').bind_text_from(
                page.model, ('metrics', 'used_pct'), backward=_pct_or_dash)
        with layout.cell('avail_card'):
            info_card('AVAILABLE', '--').bind_text_from(
                page.model, ('metrics', 'avail_gb'), backward=_gb_or_dash)
        with layout.cell('total_card'):
            info_card('TOTAL SIZE', '--').bind_text_from(
                page.model, ('metrics', 'size_gb'), backward=_gb_or_dash)
        with layout.cell('chart'):
            history_chart(page, f'USAGE HISTORY — {self.path} (%)', self.id, 'used_pct')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            pct = page.model.metrics.get('used_pct')
            if pct is not None:
                color = STATUS_COLORS['failed'] if pct >= self.threshold else STATUS_COLORS['online']
                usage_label.style(f'color: {color}')

        page.on_refresh(update_color)
        page.start()
