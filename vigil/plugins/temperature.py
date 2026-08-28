"""Thermal zone temperatures, read from /sys/class/thermal."""

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for as _level_for


def _sanitize_zone(name: str) -> str:
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


class Temperature(SignalPlugin):
    """Thermal-zone temperatures from /sys/class/thermal, reporting the hottest
    zone as the status plus one metric per zone. A host with no thermal zones —
    a VM, typically — stays online with no metric rather than reporting a
    problem it cannot see."""

    _QUERY = (
        "for d in /sys/class/thermal/thermal_zone*; do "
        "  [ -f \"$d/temp\" ] || continue; "
        "  type=$(cat \"$d/type\" 2>/dev/null || echo unknown); "
        "  temp=$(cat \"$d/temp\" 2>/dev/null || echo 0); "
        "  echo \"SENSOR:${type}:${temp}\"; "
        "done"
    )

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning   = float(config.get('warning',   70))
        self.threshold = float(config.get('threshold', 80))

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._item_color_rule = f'temp_zone_{self.id}'
        register_item_color_rule(self._item_color_rule)(
            lambda item: _level_for(item.get('value') or 0.0, self.warning, self.threshold))
        self._color_rule = f'temp_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command(self._QUERY)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Temperature collection failed: {stderr}")

        sensors: Dict[str, float] = {}
        for line in stdout.splitlines():
            if not line.startswith('SENSOR:'):
                continue
            parts = line.split(':', 2)
            if len(parts) != 3:
                continue
            zone_type, temp_mc = parts[1], parts[2].strip()
            try:
                temp_c = int(temp_mc) / 1000.0
            except (ValueError, TypeError):
                continue
            key = _sanitize_zone(zone_type)
            sensors[key] = max(sensors.get(key, 0.0), temp_c)

        if not sensors:
            return CollectResult(logs=[("No thermal zones found — skipping", "INFO")],
                                 status='online')

        max_temp = max(sensors.values())
        metrics = {'temp_c': max_temp}
        for key, temp_c in sensors.items():
            metrics[f'temp_zone_{key}'] = temp_c

        status = _level_for(max_temp, self.warning, self.threshold)
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"Max {max_temp:.1f}°C across {len(sensors)} zone(s) "
                f"(warn {self.warning:g}°C / fail {self.threshold:g}°C)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'temp_card': {'metric': 'temp_c', 'title': 'MAX TEMP', 'format': 'temp_c1',
                          'color': self._color_rule},
            'sensors': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'temp_zone_', 'metrics_suffix': '',
                    'item_format': 'temp_c1',
                    'item_color_by': self._item_color_rule,
                    'label_transform': 'spaces_upper',
                    'container': 'cards',
                    'empty_text': 'No thermal zones found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['temp_card']

    def rows(self) -> List[List[str]]:
        return [['sensors']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'temp_chart': {'metric': 'temp_c', 'title': 'TEMPERATURE (°C)'}}
