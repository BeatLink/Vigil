from typing import Dict, Any, Optional
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart
from vigil.core.ui.theme import STATUS_COLORS

_COLLECT_CMD = (
    "for f in /sys/class/thermal/thermal_zone*/temp; "
    "do [ -f \"$f\" ] && echo \"TEMP:$(cat $f)\"; done"
)


def _level_for(value: float, warning: float, failed: float) -> str:
    if value >= failed:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


_DEFAULT_LAYOUT = [
    ['host_card', 'temp_card'],
    ['chart'],
    ['logs'],
]


class TemperaturePlugin(BasePlugin):
    """
    Monitors CPU/system temperature over SSH via /sys/class/thermal/thermal_zone*/temp.

    Reports the maximum temperature across all thermal zones (in millidegrees Celsius,
    converted to °C). Gracefully stays online when no thermal zones are present
    (e.g. VMs), logging a notice instead of failing.

    Config options:
      temp_warning   °C that triggers warning (default: 70)
      temp_threshold °C that triggers failed  (default: 80)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.temp_warning   = int(config.get('temp_warning',   70))
        self.temp_threshold = int(config.get('temp_threshold', 80))
        self.ssh_collector = self.internal_modules['collectors']['ssh']
        self.db_logger     = self.internal_modules['loggers']['db_logs']
        self.db_metrics    = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        temp_lines = [l for l in stdout.splitlines() if l.startswith('TEMP:')]

        if not temp_lines:
            self.db_logger.write("No thermal zones found — skipping temperature check", level="INFO")
            self.set_status('online')
            return

        try:
            temps_mc = [int(l.removeprefix('TEMP:')) for l in temp_lines
                        if l.removeprefix('TEMP:').strip().isdigit()]
            if not temps_mc:
                self.db_logger.write("Could not parse any thermal zone values", level="ERROR")
                self.set_status('failed')
                return
            temp_c = max(temps_mc) / 1000.0
        except (ValueError, IndexError) as e:
            self.db_logger.write(f"Failed to parse output: {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('temp_c', temp_c)

        overall = _level_for(temp_c, self.temp_warning, self.temp_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"TEMP {temp_c:.1f}°C across {len(temps_mc)} zone(s) "
            f"(warn {self.temp_warning}°C / fail {self.temp_threshold}°C)",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        def latest(metric_name):
            return Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).first()

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('temp_card'):
            temp_label = info_card('TEMP', 'N/A')
        with layout.cell('chart'):
            history_chart('TEMPERATURE (°C)', self.name, 'temp_c')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            temp = latest('temp_c')
            if temp:
                temp_label.text = f'{temp.value:.1f}°C'
                temp_label.style(f'color: {STATUS_COLORS[_level_for(temp.value, self.temp_warning, self.temp_threshold)]}')

        ui.timer(5.0, update_cards)
