"""Memory and swap use, read from /proc/meminfo."""

from typing import Any, Dict, List

from vigil.plugins.base.signal_plugin import (
    LOG_LEVEL as _LOG_LEVEL, SignalPlugin,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for as _level_for, format_bytes as _fmt_gb


def _format_memory_used(values: Dict[str, Any]) -> str:
    used, total = values.get('memory_used_gb'), values.get('memory_total_gb')
    if used is None or total is None:
        return '--'
    return f'{_fmt_gb(used)} / {_fmt_gb(total)}'


class Memory(SignalPlugin):
    """RAM usage from /proc/meminfo, counting MemAvailable as free so the
    filesystem cache is not reported as used."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning   = float(config.get('warning',   75))
        self.threshold = float(config.get('threshold', 90))

        from vigil.core.ui.spec import register_color_rule, threshold_color, register_item_formatter
        self._color_rule = f'memory_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))
        self._used_format = f'memory_used_{self.id}'
        register_item_formatter(self._used_format)(_format_memory_used)

    def commands(self) -> List[Command]:
        return [Command("grep -E 'MemTotal:|MemAvailable:' /proc/meminfo")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Memory collection failed: {stderr}")

        lines = stdout.splitlines()
        total_line = next((l for l in lines if l.startswith('MemTotal:')),     None)
        avail_line = next((l for l in lines if l.startswith('MemAvailable:')), None)
        if not total_line or not avail_line:
            return CollectResult.failed(f"Incomplete memory output: {stdout!r}")

        try:
            total_kb = int(total_line.split()[1])
            avail_kb = int(avail_line.split()[1])
            used_kb  = total_kb - avail_kb
            memory_pct      = 100.0 * used_kb / total_kb if total_kb > 0 else 0.0
            memory_total_gb = total_kb / (1024 ** 2)
            memory_used_gb  = used_kb  / (1024 ** 2)
        except (ValueError, IndexError, ZeroDivisionError) as e:
            return CollectResult.failed(f"Failed to parse memory output: {e}")

        status = _level_for(memory_pct, self.warning, self.threshold)
        return CollectResult(
            metrics={
                'memory_pct':      memory_pct,
                'memory_used_gb':  memory_used_gb,
                'memory_total_gb': memory_total_gb,
            },
            logs=[(
                f"MEM {memory_pct:.1f}% ({_fmt_gb(memory_used_gb)} / {_fmt_gb(memory_total_gb)}, "
                f"warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'mem_pct_card': {'metric': 'memory_pct', 'title': 'MEMORY',
                             'format': 'percent1_plain_dash', 'color': self._color_rule},
            'mem_used_card': {'title': 'MEM USED', 'metrics': ['memory_used_gb', 'memory_total_gb'],
                              'format_fn': self._used_format},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'memory_chart': {'metric': 'memory_pct', 'title': 'MEMORY USAGE (%)'}}
