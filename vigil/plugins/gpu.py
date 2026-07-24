from typing import Any, Dict, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_COLLECT_CMD = (
    "nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits"
)

_DEFAULT_LAYOUT = [
    ['host_card', 'util_card', 'mem_card', 'temp_card'],
    ['gpus'],
    ['chart'],
    ['events'],
]


class GpuCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.util_warning   = int(config.get('util_warning',   85))
        self.util_threshold = int(config.get('util_threshold', 95))
        self.mem_warning    = int(config.get('mem_warning',    85))
        self.mem_threshold  = int(config.get('mem_threshold',  95))
        self.temp_warning   = int(config.get('temp_warning',   80))
        self.temp_threshold = int(config.get('temp_threshold', 90))

    def commands(self) -> List[Command]:
        return [Command(_COLLECT_CMD)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

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


class GpuUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.util_warning   = int(config.get('util_warning',   85))
        self.util_threshold = int(config.get('util_threshold', 95))
        self.mem_warning    = int(config.get('mem_warning',    85))
        self.mem_threshold  = int(config.get('mem_threshold',  95))
        self.temp_warning   = int(config.get('temp_warning',   80))
        self.temp_threshold = int(config.get('temp_threshold', 90))

        from vigil.web.ui.spec import register_item_color_rule, register_color_rule, threshold_color
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
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
