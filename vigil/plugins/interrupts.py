from typing import Dict, Any, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for


def _extract_counter(block: str, key: str) -> Optional[int]:
    """Return the first value on the `key` line of a /proc/stat block.

    For `intr` this is the total of all interrupts since boot; for `ctxt` it is
    the total context switches. Returns None if the line is absent/malformed.
    """
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
    """
    Monitors hardware interrupt and context-switch rates over SSH via
    /proc/stat — no extra tools required on the target.

    Takes two /proc/stat snapshots one second apart and reports the per-second
    rate of interrupts (`intr`) and context switches (`ctxt`). Status is driven
    by the interrupt rate against configurable ceilings, which can flag runaway
    hardware or a misbehaving driver.

    Config options:
      irq_warning    Interrupts/sec that triggers warning (default: 20000)
      irq_threshold  Interrupts/sec that triggers failed  (default: 50000)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.irq_warning   = int(config.get('irq_warning',   20000))
        self.irq_threshold = int(config.get('irq_threshold', 50000))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/stat && sleep 1 && echo '---SNAP---' && cat /proc/stat"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/stat: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            self.db_logger.write("Unexpected /proc/stat output format", level="ERROR")
            self.set_status('failed')
            return

        intr1 = _extract_counter(halves[0], 'intr')
        intr2 = _extract_counter(halves[1], 'intr')
        ctxt1 = _extract_counter(halves[0], 'ctxt')
        ctxt2 = _extract_counter(halves[1], 'ctxt')

        if intr1 is None or intr2 is None:
            self.db_logger.write("Could not read 'intr' from /proc/stat", level="ERROR")
            self.set_status('failed')
            return

        # Clamp to guard against a counter reset between samples (e.g. reboot).
        irq_rate = max(0.0, float(intr2 - intr1))
        self.db_metrics.metric('irq_per_sec', irq_rate)

        if ctxt1 is not None and ctxt2 is not None:
            self.db_metrics.metric('ctxt_per_sec', max(0.0, float(ctxt2 - ctxt1)))

        overall = _level_for(irq_rate, self.irq_warning, self.irq_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"{irq_rate:.0f} interrupts/sec (warn {self.irq_warning} / fail {self.irq_threshold})",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class InterruptsUIPlugin(UIPlugin):
    """
    Dashboard rendering for the interrupts monitor — mixed: irq_card fits
    UI_SPEC (single metric, config-dependent threshold color) but the page
    has TWO charts (irq_chart/ctxt_chart), which UI_SPEC's single 'chart' key
    doesn't support, so this stays a manual layout+page build reusing the
    shared 'count_comma_rounded' formatter and a per-instance threshold rule.
    """

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
        page = self.page(metric_names=['irq_per_sec', 'ctxt_per_sec'])

        rate_formatter = FORMATTERS['count_comma_rounded']
        color_rule = COLOR_RULES[self._color_rule_name]

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
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
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            irq = page.model.metrics.get('irq_per_sec')
            if irq is not None:
                state = color_rule(irq)
                if state is not None:
                    irq_label.style(f'color: {STATUS_COLORS[state]}')

        page.on_refresh(update_color)
        page.start()
