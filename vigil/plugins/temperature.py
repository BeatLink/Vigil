from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

_COLLECT_CMD = (
    "for d in /sys/class/thermal/thermal_zone*; do "
    "  [ -f \"$d/temp\" ] || continue; "
    "  type=$(cat \"$d/type\" 2>/dev/null || echo unknown); "
    "  temp=$(cat \"$d/temp\" 2>/dev/null || echo 0); "
    "  echo \"SENSOR:${type}:${temp}\"; "
    "done"
)


def _sanitize(name: str) -> str:
    """Convert a thermal zone type name to a safe metric name suffix."""
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


from vigil.core.common.plugin_utils import level_for as _level_for


_DEFAULT_LAYOUT = [
    ['host_card', 'max_card'],
    ['sensors'],
    ['chart'],
    ['events'],
]


class TemperaturePlugin(BasePlugin):
    """
    Monitors CPU/system temperature over SSH via /sys/class/thermal/thermal_zone*.

    Stores a metric per thermal zone (temp_zone_<type>) and the overall maximum
    as temp_c (used for the history chart and status). Zone types with duplicate
    names keep the highest reading.

    Config options:
      temp_warning   °C that triggers warning (default: 70)
      temp_threshold °C that triggers failed  (default: 80)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.temp_warning   = int(config.get('temp_warning',   70))
        self.temp_threshold = int(config.get('temp_threshold', 80))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        sensors: Dict[str, float] = {}
        for line in stdout.splitlines():
            if not line.startswith('SENSOR:'):
                continue
            parts = line.split(':', 2)
            if len(parts) != 3:
                continue
            zone_type, temp_mc_str = parts[1], parts[2].strip()
            try:
                temp_c = int(temp_mc_str) / 1000.0
                key = _sanitize(zone_type)
                sensors[key] = max(sensors.get(key, 0.0), temp_c)
            except (ValueError, TypeError):
                continue

        if not sensors:
            self.db_logger.write("No thermal zones found — skipping", level="INFO")
            self.set_status('online')
            return

        max_temp = max(sensors.values())
        self.db_metrics.metric('temp_c', max_temp)
        for key, temp_c in sensors.items():
            self.db_metrics.metric(f'temp_zone_{key}', temp_c)

        overall = _level_for(max_temp, self.temp_warning, self.temp_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"Max {max_temp:.1f}°C across {len(sensors)} zone(s) "
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

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT)
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('max_card'):
            max_label = info_card('MAX TEMP', '--')
        with layout.cell('sensors'):
            sensor_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('chart'):
            history_chart('TEMPERATURE (°C)', self.id, 'temp_c')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update():
            # Gather latest value per zone (one query, deduplicated in Python)
            zone_values: Dict[str, float] = {}
            for row in (
                Metric.select()
                .where(
                    (Metric.collector == self.id) &
                    (Metric.metric_name.startswith('temp_zone_'))
                )
                .order_by(Metric.timestamp.desc())
                .limit(100)
            ):
                if row.metric_name not in zone_values:
                    zone_values[row.metric_name] = row.value

            # Rebuild sensor cards
            sensor_container.clear()
            with sensor_container:
                for metric_name in sorted(zone_values):
                    val = zone_values[metric_name]
                    display = metric_name.removeprefix('temp_zone_').replace('_', ' ').upper()
                    lbl = info_card(display, f'{val:.1f}°C')
                    lbl.style(f'color: {STATUS_COLORS[_level_for(val, self.temp_warning, self.temp_threshold)]}')

            # Update max card
            latest_max = (
                Metric.select()
                .where((Metric.collector == self.id) & (Metric.metric_name == 'temp_c'))
                .order_by(Metric.timestamp.desc())
                .first()
            )
            if latest_max:
                max_label.text = f'{latest_max.value:.1f}°C'
                max_label.style(f'color: {STATUS_COLORS[_level_for(latest_max.value, self.temp_warning, self.temp_threshold)]}')

        update()
        safe_timer(5.0, update)
