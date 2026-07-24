import re
from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_ARRAY_RE = re.compile(r'^(md\d+)\s*:\s*(\S+)\s+(\S+)', re.MULTILINE)
_STATE_RE = re.compile(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]')
_RECOVERY_RE = re.compile(r'(recovery|resync|reshape|check)\s*=\s*([\d.]+)%')

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'degraded_card'],
    ['arrays'],
    ['events'],
]


class RaidCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output("cat /proc/mdstat 2>&1")

        if ret != 0 and not stdout.strip():
            self.db_logger.write(f"Failed to read /proc/mdstat: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        ok = degraded = 0
        recovering = False

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
                    self.db_logger.write(
                        f"{dev}: DEGRADED [{active}/{expected}] [{flags}]", level="ERROR"
                    )
                    continue

            if recov:
                recovering = True
                self.db_logger.write(
                    f"{dev}: {recov.group(1)} {recov.group(2)}% in progress", level="WARNING"
                )
                ok += 1
                continue

            ok += 1
            self.db_logger.write(f"{dev}: clean", level="INFO")

        total = ok + degraded
        if total == 0:
            self.db_logger.write("No RAID arrays found in /proc/mdstat", level="WARNING")
            self.set_status('offline')
            return

        self.db_metrics.metric('arrays_total', float(total))
        self.db_metrics.metric('arrays_ok', float(ok))
        self.db_metrics.metric('arrays_degraded', float(degraded))

        if degraded > 0:
            self.set_status('failed')
        elif recovering:
            self.set_status('warning')
        else:
            self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


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
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('raid_always_online')
def _raid_ok_color(v):
    return None if v is None else 'online'


@register_color_rule('raid_nonzero_failed')
def _raid_degraded_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
