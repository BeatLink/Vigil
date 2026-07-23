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
    """Dashboard rendering for the load_average monitor — declarative, see
    UI_SPEC. Thresholds are optional (see collector docstring); when unset,
    no color rule is attached, matching the pre-UI_SPEC behavior of never
    calling update_load_1m_color.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.load_warning   = float(config['load_warning'])   if 'load_warning'   in config else None
        self.load_threshold = float(config['load_threshold']) if 'load_threshold' in config else None

        self._color_rule_name = None
        if self.load_warning is not None and self.load_threshold is not None:
            from vigil.web.ui.spec import register_color_rule, threshold_color
            self._color_rule_name = f'load_average_threshold_{self.id}'
            register_color_rule(self._color_rule_name)(
                threshold_color(warning=self.load_warning, threshold=self.load_threshold))

    @property
    def UI_SPEC(self):
        load_1m_card = {'metric': 'load_pct_1m', 'title': 'LOAD 1M', 'format': 'percent0_plain_dash'}
        if self._color_rule_name:
            load_1m_card['color'] = self._color_rule_name
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'load_1m_card': load_1m_card,
                'load_5m_card': {'metric': 'load_pct_5m', 'title': 'LOAD 5M', 'format': 'percent0_plain_dash'},
                'load_15m_card': {'metric': 'load_pct_15m', 'title': 'LOAD 15M', 'format': 'percent0_plain_dash'},
            },
            'chart': {'metric': 'load_pct_1m', 'title': 'LOAD AVERAGE (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
