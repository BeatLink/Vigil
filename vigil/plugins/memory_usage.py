from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart
from vigil.core.ui.theme import STATUS_COLORS

_COLLECT_CMD = "grep -E 'MemTotal:|MemAvailable:' /proc/meminfo"


def _level_for(value: float, warning: float, failed: float) -> str:
    if value >= failed:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def _fmt_gb(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB"


class MemoryUsagePlugin(BasePlugin):
    """
    Monitors memory usage over SSH via /proc/meminfo.

    Uses MemAvailable (not MemFree) so filesystem cache is not counted as used.
    Single SSH read — no sleep required.

    Config options:
      memory_warning   Memory % that triggers warning (default: 75)
      memory_threshold Memory % that triggers failed  (default: 90)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))
        self.ssh_collector = self.internal_modules['collectors']['ssh']
        self.db_logger     = self.internal_modules['loggers']['db_logs']
        self.db_metrics    = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        lines = stdout.splitlines()
        mem_total_line = next((l for l in lines if l.startswith('MemTotal:')),     None)
        mem_avail_line = next((l for l in lines if l.startswith('MemAvailable:')), None)

        if not mem_total_line or not mem_avail_line:
            self.db_logger.write(f"Incomplete output: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        try:
            mem_total_kb    = int(mem_total_line.split()[1])
            mem_avail_kb    = int(mem_avail_line.split()[1])
            mem_used_kb     = mem_total_kb - mem_avail_kb
            memory_pct      = 100.0 * mem_used_kb / mem_total_kb if mem_total_kb > 0 else 0.0
            memory_total_gb = mem_total_kb / (1024 ** 2)
            memory_used_gb  = mem_used_kb  / (1024 ** 2)
        except (ValueError, IndexError, ZeroDivisionError) as e:
            self.db_logger.write(f"Failed to parse output: {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('memory_pct',      memory_pct)
        self.db_metrics.metric('memory_used_gb',  memory_used_gb)
        self.db_metrics.metric('memory_total_gb', memory_total_gb)

        overall = _level_for(memory_pct, self.memory_warning, self.memory_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"MEM {memory_pct:.1f}% ({_fmt_gb(memory_used_gb)} / {_fmt_gb(memory_total_gb)}, "
            f"warn {self.memory_warning}% / fail {self.memory_threshold}%)",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric

        def latest(metric_name):
            return Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).first()

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()
            mem_pct_label  = info_card('MEMORY',   '-- %')
            mem_used_label = info_card('MEM USED', '--')

            def update_cards():
                mem_pct   = latest('memory_pct')
                mem_used  = latest('memory_used_gb')
                mem_total = latest('memory_total_gb')

                if mem_pct:
                    mem_pct_label.text = f'{mem_pct.value:.1f}%'
                    mem_pct_label.style(f'color: {STATUS_COLORS[_level_for(mem_pct.value, self.memory_warning, self.memory_threshold)]}')

                if mem_used and mem_total:
                    mem_used_label.text = f'{_fmt_gb(mem_used.value)} / {_fmt_gb(mem_total.value)}'

            ui.timer(5.0, update_cards)

        history_chart('MEMORY USAGE (%)', self.name, 'memory_pct')
        self.internal_modules['ui']['logs_table']()
