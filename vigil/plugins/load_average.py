from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

# Single SSH read — no sleep needed for load averages.
_COLLECT_CMD = 'echo "LOAD:$(cat /proc/loadavg)"; echo "CPUS:$(nproc)"'


_DEFAULT_LAYOUT = [
    ['host_card', 'load_1m_card', 'load_5m_card', 'load_15m_card'],
    ['chart'],
    ['events'],
]


class LoadAverageCollectorPlugin(CollectorPlugin):
    """
    Monitors system load averages over SSH via /proc/loadavg.

    Load values are normalized by CPU core count (via nproc) and stored as a
    percentage — 100% means the system is exactly at capacity.  Falls back to
    treating core count as 1 if nproc is unavailable.

    Thresholds are optional.  When unset, metrics are collected and displayed
    but do not affect plugin status.

    Config options:
      load_warning   1m load as % of cores that triggers warning (optional)
      load_threshold 1m load as % of cores that triggers failed  (optional)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.load_warning   = float(config['load_warning'])   if 'load_warning'   in config else None
        self.load_threshold = float(config['load_threshold'])  if 'load_threshold'  in config else None

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        lines = stdout.splitlines()
        load_line = next((l for l in lines if l.startswith('LOAD:')), None)
        cpus_line = next((l for l in lines if l.startswith('CPUS:')), None)

        if not load_line:
            self.db_logger.write(f"Incomplete output: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        try:
            cpu_count    = max(1, int(cpus_line.removeprefix('CPUS:').strip())) if cpus_line else 1
            parts        = load_line.removeprefix('LOAD:').split()
            load_pct_1m  = float(parts[0]) / cpu_count * 100.0
            load_pct_5m  = float(parts[1]) / cpu_count * 100.0
            load_pct_15m = float(parts[2]) / cpu_count * 100.0
        except (ValueError, IndexError) as e:
            self.db_logger.write(f"Failed to parse output: {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('load_pct_1m',  load_pct_1m)
        self.db_metrics.metric('load_pct_5m',  load_pct_5m)
        self.db_metrics.metric('load_pct_15m', load_pct_15m)

        if self.load_warning is not None and self.load_threshold is not None:
            overall = _level_for(load_pct_1m, self.load_warning, self.load_threshold)
        else:
            overall = 'online'

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"LOAD {load_pct_1m:.0f}% / {load_pct_5m:.0f}% / {load_pct_15m:.0f}% (1m/5m/15m, "
            f"{cpu_count} cores)",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class LoadAverageUIPlugin(UIPlugin):
    """Dashboard rendering for the load_average monitor."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['load_pct_1m', 'load_pct_5m', 'load_pct_15m'])

        load_warning   = float(self.config['load_warning'])   if 'load_warning'   in self.config else None
        load_threshold = float(self.config['load_threshold']) if 'load_threshold' in self.config else None

        def _pct_or_dash(v):
            return '--' if v is None else f'{v:.0f}%'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('load_1m_card'):
            load_1m_label = info_card('LOAD 1M', '-- %').bind_text_from(
                page.model, ('metrics', 'load_pct_1m'), backward=_pct_or_dash)
        with layout.cell('load_5m_card'):
            info_card('LOAD 5M', '-- %').bind_text_from(
                page.model, ('metrics', 'load_pct_5m'), backward=_pct_or_dash)
        with layout.cell('load_15m_card'):
            info_card('LOAD 15M', '-- %').bind_text_from(
                page.model, ('metrics', 'load_pct_15m'), backward=_pct_or_dash)
        with layout.cell('chart'):
            history_chart(page, 'LOAD AVERAGE (%)', self.id, 'load_pct_1m')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_load_1m_color():
            if load_warning is None or load_threshold is None:
                return
            value = page.model.metrics.get('load_pct_1m')
            if value is not None:
                load_1m_label.style(f'color: {STATUS_COLORS[_level_for(value, load_warning, load_threshold)]}')

        page.on_refresh(update_load_1m_color)

        page.start()
