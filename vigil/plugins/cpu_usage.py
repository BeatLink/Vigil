from typing import Dict, Any, List, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_COLLECT_CMD = (
    "{ head -1 /proc/stat; sleep 1; head -1 /proc/stat; }"
)

_SEVERITY = {'online': 0, 'warning': 1, 'failed': 2}


def _parse_cpu_line(line: str) -> Tuple[int, int]:
    parts = line.split()
    user, nice, system, idle = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
    iowait  = int(parts[5]) if len(parts) > 5 else 0
    irq     = int(parts[6]) if len(parts) > 6 else 0
    softirq = int(parts[7]) if len(parts) > 7 else 0
    steal   = int(parts[8]) if len(parts) > 8 else 0
    total   = user + nice + system + idle + iowait + irq + softirq + steal
    return total, idle + iowait


def _cpu_pct(line1: str, line2: str) -> float:
    total1, idle1 = _parse_cpu_line(line1)
    total2, idle2 = _parse_cpu_line(line2)
    delta_total = total2 - total1
    delta_idle  = idle2  - idle1
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - delta_idle / delta_total)))


_DEFAULT_LAYOUT = [
    ['host_card', 'cpu_card'],
    ['chart'],
    ['events'],
]


class CpuUsageCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.cpu_warning   = int(config.get('cpu_warning',   70))
        self.cpu_threshold = int(config.get('cpu_threshold', 85))

    def commands(self) -> List[Command]:
        return [Command(_COLLECT_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

        lines = stdout.splitlines()
        cpu_lines = [l for l in lines if l.startswith('cpu ')]

        if len(cpu_lines) < 2:
            return CollectResult.failed(f"Incomplete output: {stdout!r}")

        try:
            cpu_pct = _cpu_pct(cpu_lines[0], cpu_lines[1])
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse output: {e}")

        overall = _level_for(cpu_pct, self.cpu_warning, self.cpu_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics={'cpu_pct': cpu_pct},
            logs=[(
                f"CPU {cpu_pct:.1f}% (warn {self.cpu_warning}% / fail {self.cpu_threshold}%)",
                log_level,
            )],
            status=overall,
        )


class CpuUsageUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.cpu_warning   = int(config.get('cpu_warning',   70))
        self.cpu_threshold = int(config.get('cpu_threshold', 85))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'cpu_usage_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.cpu_warning, threshold=self.cpu_threshold))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'cpu_card': {
                    'metric': 'cpu_pct', 'title': 'CPU',
                    'format': 'percent1', 'color': self._color_rule_name,
                },
            },
            'chart': {'metric': 'cpu_pct', 'title': 'CPU USAGE (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
