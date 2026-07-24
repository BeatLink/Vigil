from typing import Any, Dict, List

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.web_plugin_base import UIPlugin
from vigil.core.common.plugin_helpers import level_for as _level_for

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

        from vigil.web.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._item_color_rule_name = f'temperature_zone_{self.id}'
        register_item_color_rule(self._item_color_rule_name)(
            lambda item: _level_for(item.get('value') or 0.0, self.temp_warning, self.temp_threshold))
        self._max_color_rule_name = f'temperature_max_{self.id}'
        register_color_rule(self._max_color_rule_name)(
            threshold_color(warning=self.temp_warning, threshold=self.temp_threshold))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'max_card': {'metric': 'temp_c', 'title': 'MAX TEMP', 'format': 'temp_c1',
                            'color': self._max_color_rule_name},
                'sensors': {
                    'repeat': {
                        'source': 'metrics_prefix',
                        'metrics_prefix': 'temp_zone_', 'metrics_suffix': '',
                        'item_format': 'temp_c1',
                        'item_color_by': self._item_color_rule_name,
                        'label_transform': 'spaces_upper',
                        'container': 'cards',
                        'empty_text': 'No thermal zones found',
                    },
                },
            },
            'chart': {'metric': 'temp_c', 'title': 'TEMPERATURE (°C)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
