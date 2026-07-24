from typing import Dict, Any, List
from collections import Counter

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_TCP_STATES = {
    '01': 'ESTABLISHED',
    '02': 'SYN_SENT',
    '03': 'SYN_RECV',
    '04': 'FIN_WAIT1',
    '05': 'FIN_WAIT2',
    '06': 'TIME_WAIT',
    '07': 'CLOSE',
    '08': 'CLOSE_WAIT',
    '09': 'LAST_ACK',
    '0A': 'LISTEN',
    '0B': 'CLOSING',
}


def _parse_states(stdout: str) -> Counter:
    counts: Counter = Counter()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        if fields[0].rstrip(':') == 'sl':
            continue
        state = _TCP_STATES.get(fields[3].upper())
        if state:
            counts[state] += 1
    return counts


_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'established_card', 'listen_card', 'timewait_card'],
    ['total_chart'],
    ['established_chart'],
    ['events'],
]


class ConnectionsCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.total_warning   = int(config.get('total_warning',   500))
        self.total_threshold = int(config.get('total_threshold', 1000))

    def commands(self) -> List[Command]:
        return [Command("cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/net/tcp: {stderr}")

        counts = _parse_states(stdout)
        total = sum(counts.values())

        metrics = {state.lower(): float(counts.get(state, 0)) for state in _TCP_STATES.values()}
        metrics['total'] = float(total)

        overall = _level_for(total, self.total_warning, self.total_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        summary = ', '.join(f"{s}={counts[s]}" for s in sorted(counts)) or "no connections"
        return CollectResult(
            metrics=metrics,
            logs=[(f"{total} TCP connections ({summary})", log_level)],
            status=overall,
        )


class ConnectionsUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.total_warning   = int(self.config.get('total_warning',   500))
        self.total_threshold = int(self.config.get('total_threshold', 1000))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._total_color_name = f'connections_total_{self.id}'
        register_color_rule(self._total_color_name)(
            threshold_color(warning=self.total_warning, threshold=self.total_threshold))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'total_card': {'metric': 'total', 'title': 'TOTAL', 'format': 'int_rounded',
                               'color': self._total_color_name},
                'established_card': {'metric': 'established', 'title': 'ESTABLISHED', 'format': 'int_rounded'},
                'listen_card': {'metric': 'listen', 'title': 'LISTENING', 'format': 'int_rounded'},
                'timewait_card': {'metric': 'time_wait', 'title': 'TIME_WAIT', 'format': 'int_rounded'},
            },
            'charts': {
                'total_chart': {'metric': 'total', 'title': 'TOTAL CONNECTIONS'},
                'established_chart': {'metric': 'established', 'title': 'ESTABLISHED'},
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
