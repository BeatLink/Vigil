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
    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT)
        )
        page = self.ui.page(metric_names=['gpu_util', 'gpu_mem_pct', 'gpu_temp'])

        util_warning   = int(self.config.get('util_warning',   85))
        util_threshold = int(self.config.get('util_threshold', 95))
        mem_warning    = int(self.config.get('mem_warning',    85))
        mem_threshold  = int(self.config.get('mem_threshold',  95))
        temp_warning   = int(self.config.get('temp_warning',   80))
        temp_threshold = int(self.config.get('temp_threshold', 90))

        _pct_or_dash = FORMATTERS['percent0']
        _temp_or_dash = FORMATTERS['temp_c0']

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('util_card'):
            util_label = info_card('GPU', '-- %').bind_text_from(
                page.model, ('metrics', 'gpu_util'), backward=_pct_or_dash)
        with layout.cell('mem_card'):
            mem_label = info_card('VRAM', '-- %').bind_text_from(
                page.model, ('metrics', 'gpu_mem_pct'), backward=_pct_or_dash)
        with layout.cell('temp_card'):
            temp_label = info_card('TEMP', '--').bind_text_from(
                page.model, ('metrics', 'gpu_temp'), backward=_temp_or_dash)
        with layout.cell('gpus'):
            gpu_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('chart'):
            history_chart(page, 'GPU UTILIZATION (%)', self.id, 'gpu_util')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update():
            util = page.model.metrics.get('gpu_util')
            mem  = page.model.metrics.get('gpu_mem_pct')
            temp = page.model.metrics.get('gpu_temp')
            if util is not None:
                util_label.style(f"color: {STATUS_COLORS[_level_for(util, util_warning, util_threshold)]}")
            if mem is not None:
                mem_label.style(f"color: {STATUS_COLORS[_level_for(mem, mem_warning, mem_threshold)]}")
            if temp is not None:
                temp_label.style(f"color: {STATUS_COLORS[_level_for(temp, temp_warning, temp_threshold)]}")

            gpu_util: Dict[str, float] = {}
            for row in (
                Metric.select()
                .where(
                    (Metric.collector == self.id) &
                    (Metric.metric_name.startswith('gpu')) &
                    (Metric.metric_name.endswith('_util')) &
                    (Metric.metric_name != 'gpu_util')
                )
                .order_by(Metric.timestamp.desc())
                .limit(100)
            ):
                if row.metric_name not in gpu_util:
                    gpu_util[row.metric_name] = row.value

            gpu_container.clear()
            with gpu_container:
                for metric_name in sorted(gpu_util):
                    val = gpu_util[metric_name]
                    idx = metric_name.removeprefix('gpu').removesuffix('_util')
                    lbl = info_card(f'GPU {idx}', f'{val:.0f}%')
                    lbl.style(f'color: {STATUS_COLORS[_level_for(val, util_warning, util_threshold)]}')

        page.on_refresh(update)
        update()
        page.start()
