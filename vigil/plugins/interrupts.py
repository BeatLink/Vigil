"""Interrupt and context-switch rates, read from /proc/stat."""

from typing import Any, Dict, List, Optional

from vigil.plugins.base.signal_plugin import SignalPlugin
from vigil.core.connectors.types import CmdResult, CollectResult, Command, Status
from vigil.core.settings.config_schema import PluginConfig
from vigil.plugins.base.plugin_helpers import level_for


def _extract_counter(block: str, key: str) -> Optional[int]:
    for line in block.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


class Interrupts(SignalPlugin):
    """Interrupt and context-switch rates from two /proc/stat samples a second
    apart. Takes its own sample rather than sharing the cpu monitor's, so a
    host can run either without the other."""

    def __init__(self, name: str, config: PluginConfig):
        super().__init__(name, config)
        self.warning   = float(config.get('warning',   20000))
        self.threshold = float(config.get('threshold', 50000))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule = f'interrupts_{self.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    SAMPLED = True

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

        status = level_for(irq_rate, self.warning, self.threshold)
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{irq_rate:.0f} interrupts/sec (warn {self.warning:g} / fail {self.threshold:g})",
                Status(status).log_level,
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'irq_card': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS/S',
                         'format': 'count_comma_rounded', 'color': self._color_rule},
            'ctxt_card': {'metric': 'ctxt_per_sec', 'title': 'CTX SWITCH/S',
                          'format': 'count_comma_rounded'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'irq_chart': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS / SEC'},
            'ctxt_chart': {'metric': 'ctxt_per_sec', 'title': 'CONTEXT SWITCHES / SEC'},
        }
