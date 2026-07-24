from typing import Any, Dict, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_COLLECT_CMD = (
    "for d in /sys/class/thermal/thermal_zone*; do "
    "  [ -f \"$d/temp\" ] || continue; "
    "  type=$(cat \"$d/type\" 2>/dev/null || echo unknown); "
    "  temp=$(cat \"$d/temp\" 2>/dev/null || echo 0); "
    "  echo \"SENSOR:${type}:${temp}\"; "
    "done"
)


def _sanitize(name: str) -> str:
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


_DEFAULT_LAYOUT = [
    ['host_card', 'max_card'],
    ['sensors'],
    ['chart'],
    ['events'],
]


class TemperatureCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.temp_warning   = int(config.get('temp_warning',   70))
        self.temp_threshold = int(config.get('temp_threshold', 80))

    def commands(self) -> List[Command]:
        return [Command(_COLLECT_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

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
            return CollectResult(logs=[("No thermal zones found — skipping", "INFO")], status='online')

        max_temp = max(sensors.values())
        metrics = {'temp_c': max_temp}
        for key, temp_c in sensors.items():
            metrics[f'temp_zone_{key}'] = temp_c

        overall = _level_for(max_temp, self.temp_warning, self.temp_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"Max {max_temp:.1f}°C across {len(sensors)} zone(s) "
                f"(warn {self.temp_warning}°C / fail {self.temp_threshold}°C)",
                log_level,
            )],
            status=overall,
        )


class TemperatureUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.temp_warning   = int(self.config.get('temp_warning',   70))
        self.temp_threshold = int(self.config.get('temp_threshold', 80))

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT)
        )
        page = self.ui.page(metric_names=['temp_c'])

        _temp_or_dash = FORMATTERS['temp_c1']

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('max_card'):
            max_label = info_card('MAX TEMP', '--').bind_text_from(
                page.model, ('metrics', 'temp_c'), backward=_temp_or_dash)
        with layout.cell('sensors'):
            sensor_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('chart'):
            history_chart(page, 'TEMPERATURE (°C)', self.id, 'temp_c')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update_max_color():
            val = page.model.metrics.get('temp_c')
            if val is not None:
                max_label.style(f'color: {STATUS_COLORS[_level_for(val, self.temp_warning, self.temp_threshold)]}')

        def update_sensors():
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

            sensor_container.clear()
            with sensor_container:
                for metric_name in sorted(zone_values):
                    val = zone_values[metric_name]
                    display = metric_name.removeprefix('temp_zone_').replace('_', ' ').upper()
                    lbl = info_card(display, f'{val:.1f}°C')
                    lbl.style(f'color: {STATUS_COLORS[_level_for(val, self.temp_warning, self.temp_threshold)]}')

        page.on_refresh(update_max_color)
        page.on_refresh(update_sensors)
        update_sensors()

        page.start()
