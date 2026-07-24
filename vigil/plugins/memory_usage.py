from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for, format_bytes as _fmt_gb

_COLLECT_CMD = "grep -E 'MemTotal:|MemAvailable:' /proc/meminfo"


_DEFAULT_LAYOUT = [
    ['host_card', 'mem_pct_card', 'mem_used_card'],
    ['chart'],
    ['events'],
]


class MemoryUsageCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))

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


class MemoryUsageUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'memory_usage_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.memory_warning, threshold=self.memory_threshold))

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS, COLOR_RULES
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['memory_pct', 'memory_used_gb', 'memory_total_gb'])

        pct_formatter = FORMATTERS['percent1_plain_dash']
        color_rule = COLOR_RULES[self._color_rule_name]

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('mem_pct_card'):
            mem_pct_label = info_card('MEMORY', pct_formatter(None)).bind_text_from(
                page.model, ('metrics', 'memory_pct'), backward=pct_formatter)
        with layout.cell('mem_used_card'):
            mem_used_label = info_card('MEM USED', '--')
        with layout.cell('chart'):
            history_chart(page, 'MEMORY USAGE (%)', self.id, 'memory_pct')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update():
            value = page.model.metrics.get('memory_pct')
            if value is not None:
                state = color_rule(value)
                if state is not None:
                    mem_pct_label.style(f'color: {STATUS_COLORS[state]}')

            mem_used  = page.model.metrics.get('memory_used_gb')
            mem_total = page.model.metrics.get('memory_total_gb')
            if mem_used is not None and mem_total is not None:
                mem_used_label.text = f'{_fmt_gb(mem_used)} / {_fmt_gb(mem_total)}'

        page.on_refresh(update)
        page.start()
