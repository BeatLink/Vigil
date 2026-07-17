from typing import Dict, Any
from collections import Counter

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.plugin_utils import level_for as _level_for
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# Linux TCP connection state codes as they appear (hex) in /proc/net/tcp[6].
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
    """Count TCP connections per state name from concatenated /proc/net/tcp[6].

    Each data row's 4th whitespace-delimited column is the connection state
    as a two-digit hex code. Header rows (starting with 'sl') are ignored.
    """
    counts: Counter = Counter()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        if fields[0].rstrip(':') == 'sl':  # header row
            continue
        state = _TCP_STATES.get(fields[3].upper())
        if state:
            counts[state] += 1
    return counts


_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'established_card', 'listen_card', 'timewait_card'],
    ['total_chart'],
    ['established_chart'],
    ['logs'],
]


class ConnectionsPlugin(BasePlugin):
    """
    Monitors the count of TCP connections by state over SSH, reading
    /proc/net/tcp and /proc/net/tcp6 — no netstat/ss required on the target.

    A metric is recorded per state (established, time_wait, listen, ...) plus a
    total. Status is driven by the total connection count against configurable
    warning/critical ceilings — useful for spotting connection leaks or floods.

    Config options:
      total_warning    Total connection count that triggers warning (default: 500)
      total_threshold  Total connection count that triggers failed  (default: 1000)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.total_warning   = int(config.get('total_warning',   500))
        self.total_threshold = int(config.get('total_threshold', 1000))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null"
        )
        if ret != 0:
            self.db_logger.write(f"Failed to read /proc/net/tcp: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        counts = _parse_states(stdout)
        total = sum(counts.values())

        # Record one metric per named state so history charts can target any of them.
        for state in _TCP_STATES.values():
            self.db_metrics.metric(state.lower(), float(counts.get(state, 0)))
        self.db_metrics.metric('total', float(total))

        overall = _level_for(total, self.total_warning, self.total_threshold)
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        summary = ', '.join(f"{s}={counts[s]}" for s in sorted(counts)) or "no connections"
        self.db_logger.write(f"{total} TCP connections ({summary})", level=log_level)
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            total_label = info_card('TOTAL', '--')
        with layout.cell('established_card'):
            established_label = info_card('ESTABLISHED', '--')
        with layout.cell('listen_card'):
            listen_label = info_card('LISTENING', '--')
        with layout.cell('timewait_card'):
            timewait_label = info_card('TIME_WAIT', '--')
        with layout.cell('total_chart'):
            history_chart('TOTAL CONNECTIONS', self.name, 'total')
        with layout.cell('established_chart'):
            history_chart('ESTABLISHED', self.name, 'established')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            total = self.latest_metric('total')
            established = self.latest_metric('established')
            listen = self.latest_metric('listen')
            timewait = self.latest_metric('time_wait')
            if total:
                total_label.text = f'{total.value:.0f}'
                total_label.style(f'color: {STATUS_COLORS[_level_for(total.value, self.total_warning, self.total_threshold)]}')
            if established:
                established_label.text = f'{established.value:.0f}'
            if listen:
                listen_label.text = f'{listen.value:.0f}'
            if timewait:
                timewait_label.text = f'{timewait.value:.0f}'

        safe_timer(5.0, update_cards)
