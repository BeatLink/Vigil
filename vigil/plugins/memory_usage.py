from typing import Any, Dict, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for, format_bytes as _fmt_gb

_COLLECT_CMD = "grep -E 'MemTotal:|MemAvailable:' /proc/meminfo"


_DEFAULT_LAYOUT = [
    ['host_card', 'mem_pct_card', 'mem_used_card'],
    ['chart'],
    ['events'],
]


class MemoryUsageCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))

    def commands(self) -> List[Command]:
        return [Command(_COLLECT_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

        lines = stdout.splitlines()
        mem_total_line = next((l for l in lines if l.startswith('MemTotal:')),     None)
        mem_avail_line = next((l for l in lines if l.startswith('MemAvailable:')), None)

        if not mem_total_line or not mem_avail_line:
            return CollectResult.failed(f"Incomplete output: {stdout!r}")

        try:
            mem_total_kb    = int(mem_total_line.split()[1])
            mem_avail_kb    = int(mem_avail_line.split()[1])
            mem_used_kb     = mem_total_kb - mem_avail_kb
            memory_pct      = 100.0 * mem_used_kb / mem_total_kb if mem_total_kb > 0 else 0.0
            memory_total_gb = mem_total_kb / (1024 ** 2)
            memory_used_gb  = mem_used_kb  / (1024 ** 2)
        except (ValueError, IndexError, ZeroDivisionError) as e:
            return CollectResult.failed(f"Failed to parse output: {e}")

        metrics = {
            'memory_pct':      memory_pct,
            'memory_used_gb':  memory_used_gb,
            'memory_total_gb': memory_total_gb,
        }

        overall = _level_for(memory_pct, self.memory_warning, self.memory_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"MEM {memory_pct:.1f}% ({_fmt_gb(memory_used_gb)} / {_fmt_gb(memory_total_gb)}, "
                f"warn {self.memory_warning}% / fail {self.memory_threshold}%)",
                log_level,
            )],
            status=overall,
        )


class MemoryUsageUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))

        from vigil.web.ui.spec import register_color_rule, threshold_color, register_item_formatter
        self._color_rule_name = f'memory_usage_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.memory_warning, threshold=self.memory_threshold))
        self._used_format_name = f'memory_usage_used_{self.id}'
        register_item_formatter(self._used_format_name)(self._format_used)

    @staticmethod
    def _format_used(values: Dict[str, Any]) -> str:
        used, total = values.get('memory_used_gb'), values.get('memory_total_gb')
        if used is None or total is None:
            return '--'
        return f'{_fmt_gb(used)} / {_fmt_gb(total)}'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'mem_pct_card': {'metric': 'memory_pct', 'title': 'MEMORY', 'format': 'percent1_plain_dash',
                                 'color': self._color_rule_name},
                'mem_used_card': {'title': 'MEM USED', 'metrics': ['memory_used_gb', 'memory_total_gb'],
                                  'format_fn': self._used_format_name},
            },
            'chart': {'metric': 'memory_pct', 'title': 'MEMORY USAGE (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
