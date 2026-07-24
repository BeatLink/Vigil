from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import level_for as _level_for


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


class Interrupts(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.irq_warning   = int(config.get('irq_warning',   20000))
        self.irq_threshold = int(config.get('irq_threshold', 50000))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'interrupts_threshold_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.irq_warning, threshold=self.irq_threshold))

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

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'irq_card': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS/S', 'format': 'count_comma_rounded',
                            'color': self._color_rule_name},
                'ctxt_card': {'metric': 'ctxt_per_sec', 'title': 'CTX SWITCH/S', 'format': 'count_comma_rounded'},
            },
            'charts': {
                'irq_chart': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS / SEC'},
                'ctxt_chart': {'metric': 'ctxt_per_sec', 'title': 'CONTEXT SWITCHES / SEC'},
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
