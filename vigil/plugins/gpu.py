from typing import Dict, Any
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import level_for as _level_for
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# Query utilization, memory, and temperature for every GPU in one call.
# --format=csv,noheader,nounits yields bare numbers, one line per GPU:
#   <index>, <util%>, <mem_used_MiB>, <mem_total_MiB>, <temp_C>
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


class GpuPlugin(BasePlugin):
    """
    Monitors NVIDIA GPU utilization, memory, and temperature over SSH via nvidia-smi.

    Stores per-GPU metrics (gpu<idx>_util, gpu<idx>_mem_pct, gpu<idx>_temp) plus
    the busiest-GPU maxima (gpu_util, gpu_mem_pct, gpu_temp) which drive the
    history chart and overall status. Status is the worst of the utilization,
    memory, and temperature levels across all GPUs.

    Requires nvidia-smi on the target (NVIDIA proprietary/open driver). If it is
    absent the plugin reports offline rather than failed.

    Config options:
      util_warning     GPU % that triggers warning     (default: 85)
      util_threshold   GPU % that triggers failed       (default: 95)
      mem_warning      VRAM % that triggers warning     (default: 85)
      mem_threshold    VRAM % that triggers failed       (default: 95)
      temp_warning     °C that triggers warning          (default: 80)
      temp_threshold   °C that triggers failed            (default: 90)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.util_warning   = int(config.get('util_warning',   85))
        self.util_threshold = int(config.get('util_threshold', 95))
        self.mem_warning    = int(config.get('mem_warning',    85))
        self.mem_threshold  = int(config.get('mem_threshold',  95))
        self.temp_warning   = int(config.get('temp_warning',   80))
        self.temp_threshold = int(config.get('temp_threshold', 90))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)

        # nvidia-smi missing or driver not loaded — treat as "no GPU here"
        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined
                         or "couldn't communicate" in combined or 'no devices' in combined):
            self.db_logger.write("nvidia-smi unavailable or no NVIDIA GPU present", level="WARNING")
            self.set_status('offline')
            return
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

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
            self.db_metrics.metric(f'gpu{idx}_util', util)
            self.db_metrics.metric(f'gpu{idx}_mem_pct', mem_pct)
            self.db_metrics.metric(f'gpu{idx}_temp', temp)

            max_util    = max(max_util, util)
            max_mem_pct = max(max_mem_pct, mem_pct)
            max_temp    = max(max_temp, temp)
            count += 1

        if count == 0:
            self.db_logger.write(f"No GPUs parsed from output: {stdout!r}", level="WARNING")
            self.set_status('offline')
            return

        self.db_metrics.metric('gpu_util', max_util)
        self.db_metrics.metric('gpu_mem_pct', max_mem_pct)
        self.db_metrics.metric('gpu_temp', max_temp)

        levels = [
            _level_for(max_util,    self.util_warning, self.util_threshold),
            _level_for(max_mem_pct, self.mem_warning,  self.mem_threshold),
            _level_for(max_temp,    self.temp_warning, self.temp_threshold),
        ]
        severity = {'online': 0, 'warning': 1, 'failed': 2}
        overall = max(levels, key=lambda l: severity[l])

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"{count} GPU(s): peak {max_util:.0f}% util, {max_mem_pct:.0f}% VRAM, {max_temp:.0f}°C",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT)
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('util_card'):
            util_label = info_card('GPU', '-- %')
        with layout.cell('mem_card'):
            mem_label = info_card('VRAM', '-- %')
        with layout.cell('temp_card'):
            temp_label = info_card('TEMP', '--')
        with layout.cell('gpus'):
            gpu_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
            )
        with layout.cell('chart'):
            history_chart('GPU UTILIZATION (%)', self.id, 'gpu_util')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update():
            def _val(name):
                m = self.latest_metric(name)
                return m.value if m else None

            util = _val('gpu_util')
            mem  = _val('gpu_mem_pct')
            temp = _val('gpu_temp')
            if util is not None:
                util_label.text = f'{util:.0f}%'
                util_label.style(f"color: {STATUS_COLORS[_level_for(util, self.util_warning, self.util_threshold)]}")
            if mem is not None:
                mem_label.text = f'{mem:.0f}%'
                mem_label.style(f"color: {STATUS_COLORS[_level_for(mem, self.mem_warning, self.mem_threshold)]}")
            if temp is not None:
                temp_label.text = f'{temp:.0f}°C'
                temp_label.style(f"color: {STATUS_COLORS[_level_for(temp, self.temp_warning, self.temp_threshold)]}")

            # Per-GPU cards: latest util for each gpu<idx>_util metric
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
                    lbl.style(f'color: {STATUS_COLORS[_level_for(val, self.util_warning, self.util_threshold)]}')

        update()
        safe_timer(5.0, update)
