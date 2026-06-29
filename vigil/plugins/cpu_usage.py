from typing import Dict, Any, Optional, Tuple
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart
from vigil.core.ui.theme import STATUS_COLORS

_COLLECT_CMD = (
    "{ head -1 /proc/stat; sleep 1; head -1 /proc/stat; }"
)

_SEVERITY = {'online': 0, 'warning': 1, 'failed': 2}


def _parse_cpu_line(line: str) -> Tuple[int, int]:
    """Return (total_jiffies, idle_jiffies) from a /proc/stat cpu line."""
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


from vigil.core.common.plugin_utils import level_for as _level_for


_DEFAULT_LAYOUT = [
    ['host_card', 'cpu_card'],
    ['chart'],
    ['logs'],
]


class CpuUsagePlugin(BasePlugin):
    """
    Monitors CPU utilization over SSH.

    Takes two /proc/stat snapshots one second apart in a single SSH command
    and computes the usage delta — no agents or extra tools required.

    Config options:
      cpu_warning    CPU % that triggers warning (default: 70)
      cpu_threshold  CPU % that triggers failed  (default: 85)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.cpu_warning   = int(config.get('cpu_warning',   70))
        self.cpu_threshold = int(config.get('cpu_threshold', 85))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        lines = stdout.splitlines()
        cpu_lines = [l for l in lines if l.startswith('cpu ')]

        if len(cpu_lines) < 2:
            self.db_logger.write(f"Incomplete output: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        try:
            cpu_pct = _cpu_pct(cpu_lines[0], cpu_lines[1])
        except (ValueError, IndexError) as e:
            self.db_logger.write(f"Failed to parse output: {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('cpu_pct', cpu_pct)

        overall = _level_for(cpu_pct, self.cpu_warning, self.cpu_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"CPU {cpu_pct:.1f}% (warn {self.cpu_warning}% / fail {self.cpu_threshold}%)",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('cpu_card'):
            cpu_label = info_card('CPU', '-- %')
        with layout.cell('chart'):
            history_chart('CPU USAGE (%)', self.name, 'cpu_pct')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            cpu = self.latest_metric('cpu_pct')
            if cpu:
                cpu_label.text = f'{cpu.value:.1f}%'
                cpu_label.style(f'color: {STATUS_COLORS[_level_for(cpu.value, self.cpu_warning, self.cpu_threshold)]}')

        ui.timer(5.0, update_cards)
