from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for


def _extract_counter(block: str, key: str) -> Optional[int]:
    for line in block.splitlines():
        fields = line.split()
        if fields and fields[0] == key:
            try:
                return int(fields[1])
            except (ValueError, IndexError):
                return None
    return None


_DEFAULT_LAYOUT = [
    ['host_card', 'irq_card', 'ctxt_card'],
    ['irq_chart'],
    ['ctxt_chart'],
    ['events'],
]


class InterruptsCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.irq_warning   = int(config.get('irq_warning',   20000))
        self.irq_threshold = int(config.get('irq_threshold', 50000))

    def commands(self) -> List[Command]:
        return [Command("cat /proc/stat && sleep 1 && echo '---SNAP---' && cat /proc/stat")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/stat: {stderr}")

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            return CollectResult.failed("Unexpected /proc/stat output format")

        intr1 = _extract_counter(halves[0], 'intr')
        intr2 = _extract_counter(halves[1], 'intr')
        ctxt1 = _extract_counter(halves[0], 'ctxt')
        ctxt2 = _extract_counter(halves[1], 'ctxt')

        if intr1 is None or intr2 is None:
            return CollectResult.failed("Could not read 'intr' from /proc/stat")

        irq_rate = max(0.0, float(intr2 - intr1))
        metrics = {'irq_per_sec': irq_rate}

        if ctxt1 is not None and ctxt2 is not None:
            metrics['ctxt_per_sec'] = max(0.0, float(ctxt2 - ctxt1))

        overall = _level_for(irq_rate, self.irq_warning, self.irq_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{irq_rate:.0f} interrupts/sec (warn {self.irq_warning} / fail {self.irq_threshold})",
                log_level,
            )],
            status=overall,
        )


class InterruptsUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.irq_warning   = int(config.get('irq_warning',   20000))
        self.irq_threshold = int(config.get('irq_threshold', 50000))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'interrupts_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.irq_warning, threshold=self.irq_threshold))

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS, COLOR_RULES
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page(metric_names=['irq_per_sec', 'ctxt_per_sec'])

        rate_formatter = FORMATTERS['count_comma_rounded']
        color_rule = COLOR_RULES[self._color_rule_name]

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('irq_card'):
            irq_label = info_card('INTERRUPTS/S', rate_formatter(None)).bind_text_from(
                page.model, ('metrics', 'irq_per_sec'), backward=rate_formatter)
        with layout.cell('ctxt_card'):
            info_card('CTX SWITCH/S', rate_formatter(None)).bind_text_from(
                page.model, ('metrics', 'ctxt_per_sec'), backward=rate_formatter)
        with layout.cell('irq_chart'):
            history_chart(page, 'INTERRUPTS / SEC', self.id, 'irq_per_sec')
        with layout.cell('ctxt_chart'):
            history_chart(page, 'CONTEXT SWITCHES / SEC', self.id, 'ctxt_per_sec')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update_color():
            irq = page.model.metrics.get('irq_per_sec')
            if irq is not None:
                state = color_rule(irq)
                if state is not None:
                    irq_label.style(f'color: {STATUS_COLORS[state]}')

        page.on_refresh(update_color)
        page.start()
