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
    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS
        from vigil.web.ui.theme import STATUS_COLORS

        total_warning   = int(self.config.get('total_warning',   500))
        total_threshold = int(self.config.get('total_threshold', 1000))

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page(metric_names=['total', 'established', 'listen', 'time_wait'])

        _int_or_dash = FORMATTERS['int_rounded']

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('total_card'):
            total_label = info_card('TOTAL', '--').bind_text_from(
                page.model, ('metrics', 'total'), backward=_int_or_dash)
        with layout.cell('established_card'):
            info_card('ESTABLISHED', '--').bind_text_from(
                page.model, ('metrics', 'established'), backward=_int_or_dash)
        with layout.cell('listen_card'):
            info_card('LISTENING', '--').bind_text_from(
                page.model, ('metrics', 'listen'), backward=_int_or_dash)
        with layout.cell('timewait_card'):
            info_card('TIME_WAIT', '--').bind_text_from(
                page.model, ('metrics', 'time_wait'), backward=_int_or_dash)
        with layout.cell('total_chart'):
            history_chart(page, 'TOTAL CONNECTIONS', self.id, 'total')
        with layout.cell('established_chart'):
            history_chart(page, 'ESTABLISHED', self.id, 'established')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update_color():
            total = page.model.metrics.get('total')
            if total is not None:
                total_label.style(f'color: {STATUS_COLORS[_level_for(total, total_warning, total_threshold)]}')

        page.on_refresh(update_color)
        page.start()
