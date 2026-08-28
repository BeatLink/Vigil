"""CPU utilization, sampled from /proc/stat."""

from typing import Any, Dict, List, Tuple

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for as _level_for


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


class Cpu(SignalPlugin):
    """CPU utilization from two /proc/stat samples a second apart, taken on
    the target so the sleep costs one round trip rather than two."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning   = float(config.get('warning',   70))
        self.threshold = float(config.get('threshold', 85))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule = f'cpu_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    SAMPLED = True

    def commands(self) -> List[Command]:
        return [Command("{ head -1 /proc/stat; sleep 1; head -1 /proc/stat; }")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"CPU collection failed: {stderr}")

        cpu_lines = [l for l in stdout.splitlines() if l.startswith('cpu ')]
        if len(cpu_lines) < 2:
            return CollectResult.failed(f"Incomplete CPU output: {stdout!r}")

        try:
            cpu_pct = _cpu_pct(cpu_lines[0], cpu_lines[1])
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse CPU output: {e}")

        status = _level_for(cpu_pct, self.warning, self.threshold)
        return CollectResult(
            metrics={'cpu_pct': cpu_pct},
            logs=[(
                f"CPU {cpu_pct:.1f}% (warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {'cpu_card': {'metric': 'cpu_pct', 'title': 'CPU', 'format': 'percent1',
                             'color': self._color_rule}}

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'cpu_chart': {'metric': 'cpu_pct', 'title': 'CPU USAGE (%)'}}
