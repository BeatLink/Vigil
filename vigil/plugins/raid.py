import re
from typing import Any, Dict, List

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_ARRAY_RE = re.compile(r'^(md\d+)\s*:\s*(\S+)\s+(\S+)', re.MULTILINE)
_STATE_RE = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
_RECOVERY_RE = re.compile(r'(recovery|resync|reshape|check)\s*=\s*([\d.]+)%')

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['arrays'],
    ['events'],
]


class RaidCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)

    def commands(self) -> List[Command]:
        return [Command("cat /proc/mdstat 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0 and not stdout.strip():
            return CollectResult.failed(f"Failed to read /proc/mdstat: {stderr}")

        ok = degraded = 0
        recovering = False
        logs = []

        for m in _ARRAY_RE.finditer(stdout):
            dev = m.group(1)
            block = stdout[m.end():]
            next_blank = block.find('\n\n')
            block = block if next_blank < 0 else block[:next_blank]

            state = _STATE_RE.search(block)
            recov = _RECOVERY_RE.search(block)

            if state:
                expected, active, flags = int(state.group(1)), int(state.group(2)), state.group(3)
                down = flags.count('_')
                if down > 0 or active < expected:
                    degraded += 1
                    logs.append((f"{dev}: DEGRADED [{active}/{expected}] [{flags}]", "ERROR"))
                    continue

            if recov:
                recovering = True
                logs.append((f"{dev}: {recov.group(1)} {recov.group(2)}% in progress", "WARNING"))
                ok += 1
                continue

            ok += 1
            logs.append((f"{dev}: clean", "INFO"))

        total = ok + degraded
        if total == 0:
            return CollectResult.failed(
                "No RAID arrays found in /proc/mdstat", level="WARNING", status='offline')

        metrics = {
            'arrays_total': float(total),
            'arrays_ok': float(ok),
            'arrays_degraded': float(degraded),
        }

        if degraded > 0:
            status = 'failed'
        elif recovering:
            status = 'warning'
        else:
            status = 'online'

        return CollectResult(metrics=metrics, logs=logs, status=status)


class RaidUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'arrays_total', 'title': 'ARRAYS', 'format': 'int'},
            'ok_card': {
                'metric': 'arrays_ok', 'title': 'CLEAN', 'format': 'int',
                'color': 'raid_always_online',
            },
            'degraded_card': {
                'metric': 'arrays_degraded', 'title': 'DEGRADED', 'format': 'int',
                'color': 'raid_nonzero_failed',
            },
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.ui.spec import register_color_rule


@register_color_rule('raid_always_online')
def _raid_ok_color(v):
    return None if v is None else 'online'


@register_color_rule('raid_nonzero_failed')
def _raid_degraded_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
