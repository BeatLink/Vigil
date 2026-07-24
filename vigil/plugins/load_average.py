from typing import Any, Dict, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_COLLECT_CMD = 'echo "LOAD:$(cat /proc/loadavg)"; echo "CPUS:$(nproc)"'


_DEFAULT_LAYOUT = [
    ['host_card', 'load_1m_card', 'load_5m_card', 'load_15m_card'],
    ['chart'],
    ['events'],
]


class LoadAverageCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.load_warning   = float(config['load_warning'])   if 'load_warning'   in config else None
        self.load_threshold = float(config['load_threshold'])  if 'load_threshold'  in config else None

    def commands(self) -> List[Command]:
        return [Command(_COLLECT_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

        lines = stdout.splitlines()
        load_line = next((l for l in lines if l.startswith('LOAD:')), None)
        cpus_line = next((l for l in lines if l.startswith('CPUS:')), None)

        if not load_line:
            return CollectResult.failed(f"Incomplete output: {stdout!r}")

        try:
            cpu_count    = max(1, int(cpus_line.removeprefix('CPUS:').strip())) if cpus_line else 1
            parts        = load_line.removeprefix('LOAD:').split()
            load_pct_1m  = float(parts[0]) / cpu_count * 100.0
            load_pct_5m  = float(parts[1]) / cpu_count * 100.0
            load_pct_15m = float(parts[2]) / cpu_count * 100.0
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse output: {e}")

        metrics = {
            'load_pct_1m':  load_pct_1m,
            'load_pct_5m':  load_pct_5m,
            'load_pct_15m': load_pct_15m,
        }

        if self.load_warning is not None and self.load_threshold is not None:
            overall = _level_for(load_pct_1m, self.load_warning, self.load_threshold)
        else:
            overall = 'online'

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"LOAD {load_pct_1m:.0f}% / {load_pct_5m:.0f}% / {load_pct_15m:.0f}% (1m/5m/15m, "
                f"{cpu_count} cores)",
                log_level,
            )],
            status=overall,
        )


class LoadAverageUIPlugin(UIPlugin):
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
