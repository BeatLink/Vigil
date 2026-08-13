import time
from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import level_for as _level_for

_COLLECT_CMD = (
    "nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits"
)

# A wedged NVIDIA driver leaves nvidia-smi in uninterruptible sleep, where the connector's
# terminate/kill has no effect, so every poll strands another process until the host reboots.
# Repeated timeouts therefore suspend the probe instead of adding one stuck process per interval.
_COLLECT_CMD_TIMEOUT = 15.0
_TIMEOUT_TRIP = 2
_SUSPEND_SECONDS = 1800.0

_DEFAULT_LAYOUT = [
    ['host_card', 'util_card', 'mem_card', 'temp_card'],
    ['gpus'],
    ['chart'],
    ['events'],
]


class Gpu(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.util_warning   = int(config.get('util_warning',   85))
        self.util_threshold = int(config.get('util_threshold', 95))
        self.mem_warning    = int(config.get('mem_warning',    85))
        self.mem_threshold  = int(config.get('mem_threshold',  95))
        self.temp_warning   = int(config.get('temp_warning',   80))
        self.temp_threshold = int(config.get('temp_threshold', 90))
        self.timeout_trip     = int(config.get('timeout_trip',     _TIMEOUT_TRIP))
        self.suspend_seconds  = float(config.get('suspend_seconds', _SUSPEND_SECONDS))
        self._consecutive_timeouts = 0
        self._suspended_until = 0.0

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._util_color_rule_name = f'gpu_util_{self.id}'
        register_item_color_rule(self._util_color_rule_name)(
            lambda item: _level_for(item.get('value') or 0.0, self.util_warning, self.util_threshold))
        self._util_card_color_name = f'gpu_util_card_{self.id}'
        register_color_rule(self._util_card_color_name)(
            threshold_color(warning=self.util_warning, threshold=self.util_threshold))
        self._mem_card_color_name = f'gpu_mem_card_{self.id}'
        register_color_rule(self._mem_card_color_name)(
            threshold_color(warning=self.mem_warning, threshold=self.mem_threshold))
        self._temp_card_color_name = f'gpu_temp_card_{self.id}'
        register_color_rule(self._temp_card_color_name)(
            threshold_color(warning=self.temp_warning, threshold=self.temp_threshold))

    def commands(self) -> List[Command]:
        if time.monotonic() < self._suspended_until:
            return []
        return [Command(_COLLECT_CMD, timeout=_COLLECT_CMD_TIMEOUT)]

    def _suspended_result(self) -> CollectResult:
        """Reports the tripped breaker as offline, the same status an absent GPU gets."""
        minutes = self.suspend_seconds / 60.0
        return CollectResult.failed(
            f"nvidia-smi timed out {self._consecutive_timeouts}x and could not be killed — the "
            f"driver is wedged and only a reboot clears it; probe suspended for {minutes:.0f}m",
            level="WARNING", status='offline')

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not results:
            return self._suspended_result()

        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0 and 'timed out after' in stderr.lower():
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self.timeout_trip:
                self._suspended_until = time.monotonic() + self.suspend_seconds
                return self._suspended_result()
            return CollectResult.failed(f"Collection failed: {stderr}")
        self._consecutive_timeouts = 0

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined
                         or "couldn't communicate" in combined or 'no devices' in combined):
            return CollectResult.failed("nvidia-smi unavailable or no NVIDIA GPU present",
                                        level="WARNING", status='offline')
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

        metrics: Dict[str, float] = {}
        max_util = max_mem_pct = max_temp = 0.0
        count = 0
        for line in stdout.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) != 5:
                continue
            try:
                idx       = int(parts[0])
                util      = float(parts[1])
                mem_used  = float(parts[2])
                mem_total = float(parts[3])
                temp      = float(parts[4])
            except ValueError:
                continue

            mem_pct = (100.0 * mem_used / mem_total) if mem_total > 0 else 0.0
            metrics[f'gpu{idx}_util'] = util
            metrics[f'gpu{idx}_mem_pct'] = mem_pct
            metrics[f'gpu{idx}_temp'] = temp

            max_util    = max(max_util, util)
            max_mem_pct = max(max_mem_pct, mem_pct)
            max_temp    = max(max_temp, temp)
            count += 1

        if count == 0:
            return CollectResult.failed(f"No GPUs parsed from output: {stdout!r}",
                                        level="WARNING", status='offline')

        metrics['gpu_util'] = max_util
        metrics['gpu_mem_pct'] = max_mem_pct
        metrics['gpu_temp'] = max_temp

        levels = [
            _level_for(max_util,    self.util_warning, self.util_threshold),
            _level_for(max_mem_pct, self.mem_warning,  self.mem_threshold),
            _level_for(max_temp,    self.temp_warning, self.temp_threshold),
        ]
        severity = {'online': 0, 'warning': 1, 'failed': 2}
        overall = max(levels, key=lambda l: severity[l])

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{count} GPU(s): peak {max_util:.0f}% util, {max_mem_pct:.0f}% VRAM, {max_temp:.0f}°C",
                log_level,
            )],
            status=overall,
        )

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'util_card': {'metric': 'gpu_util', 'title': 'GPU', 'format': 'percent0',
                              'color': self._util_card_color_name},
                'mem_card': {'metric': 'gpu_mem_pct', 'title': 'VRAM', 'format': 'percent0',
                            'color': self._mem_card_color_name},
                'temp_card': {'metric': 'gpu_temp', 'title': 'TEMP', 'format': 'temp_c0',
                             'color': self._temp_card_color_name},
                'gpus': {
                    'repeat': {
                        'source': 'metrics_prefix',
                        'metrics_prefix': 'gpu', 'metrics_suffix': '_util',
                        'metrics_exclude': ['gpu_util'],
                        'item_format': 'percent0',
                        'item_color_by': self._util_color_rule_name,
                        'item_label_prefix': 'GPU ',
                        'container': 'cards',
                        'empty_text': 'No GPUs found',
                    },
                },
            },
            'chart': {'metric': 'gpu_util', 'title': 'GPU UTILIZATION (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
