from typing import Dict, Any, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import level_for as _level_for
from vigil.core.ui.components import info_card, history_chart, on_data_event
from vigil.core.ui.theme import STATUS_COLORS


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


class InterruptsPlugin(BasePlugin):
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

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('irq_card'):
            irq_label = info_card('INTERRUPTS/S', '--')
        with layout.cell('ctxt_card'):
            ctxt_label = info_card('CTX SWITCH/S', '--')
        with layout.cell('irq_chart'):
            history_chart('INTERRUPTS / SEC', self.id, 'irq_per_sec')
        with layout.cell('ctxt_chart'):
            history_chart('CONTEXT SWITCHES / SEC', self.id, 'ctxt_per_sec')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            irq = self.latest_metric('irq_per_sec')
            ctxt = self.latest_metric('ctxt_per_sec')
            if irq:
                irq_label.text = f'{irq.value:,.0f}'
                irq_label.style(f'color: {STATUS_COLORS[_level_for(irq.value, self.irq_warning, self.irq_threshold)]}')
            if ctxt:
                ctxt_label.text = f'{ctxt.value:,.0f}'

        on_data_event('metric', irq_label, update_cards)
