from typing import Dict, Any, List, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# Sentinel emitted by the remote probe script when a check fails. Kept distinct
# from any real millisecond value so parsing is unambiguous.
_FAIL = "FAIL"


def _safe_metric_name(label: str) -> str:
    """Turn an arbitrary check label into a safe metric-name suffix."""
    return ''.join(c if c.isalnum() else '_' for c in label.lower()).strip('_') or 'check'


def _build_probe_script(checks: List[Dict[str, Any]], timeout: int) -> str:
    """Build a single remote shell script that probes every check in turn.

    Emits one line per check: "<index> <latency_ms|FAIL>". TCP checks use
    bash's /dev/tcp (no tools required); URL checks use curl.
    """
    lines = ["set +e"]
    for i, check in enumerate(checks):
        url = check.get('url')
        if url:
            lines.append(
                f'code=$(curl -o /dev/null -s -m {timeout} -w "%{{http_code}}:%{{time_total}}" '
                f'{url!r} 2>/dev/null); '
                f'case "$code" in 2*|3*) echo "{i} $(echo "$code" | cut -d: -f2 | '
                f'awk \'{{printf "%.0f", $1*1000}}\')";; *) echo "{i} {_FAIL}";; esac'
            )
        else:
            host = check.get('host', 'localhost')
            port = check['port']
            lines.append(
                f'start=$(date +%s%3N); '
                f'if timeout {timeout} bash -c "echo > /dev/tcp/{host}/{port}" 2>/dev/null; '
                f'then end=$(date +%s%3N); echo "{i} $((end - start))"; '
                f'else echo "{i} {_FAIL}"; fi'
            )
    return '\n'.join(lines)


def _parse_results(stdout: str, count: int) -> Dict[int, Optional[float]]:
    """Parse probe output into {check_index: latency_ms or None-on-failure}."""
    results: Dict[int, Optional[float]] = {i: None for i in range(count)}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        if idx not in results:
            continue
        results[idx] = None if parts[1] == _FAIL else _try_float(parts[1])
    return results


def _try_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except ValueError:
        return None


_DEFAULT_LAYOUT = [
    ['host_card', 'up_card', 'down_card'],
    ['charts'],
    ['events'],
]


class PortsPlugin(BasePlugin):
    """
    Monitors TCP port and HTTP(S) URL reachability, probing from the remote
    host over SSH. TCP checks use bash's /dev/tcp; URL checks use curl. Each
    check records a latency metric (ms) and the plugin fails if any check is
    down.

    Config options:
      timeout   Per-check timeout in seconds (default: 5)
      checks:   list of checks, each either a TCP or a URL probe:
        - name: "Web"        # optional label (defaults to host:port or url)
          host: "10.0.0.1"   # TCP: target host (default: localhost)
          port: 443          # TCP: target port
        - name: "API"
          url: "https://api.example.com/health"   # URL: HTTP(S) endpoint
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.timeout = int(config.get('timeout', 5))
        self.checks: List[Dict[str, Any]] = config.get('checks', [])
        # Precompute a stable label + metric name for each check.
        for check in self.checks:
            if 'name' not in check:
                check['name'] = check['url'] if check.get('url') else f"{check.get('host', 'localhost')}:{check.get('port')}"
            check['metric'] = _safe_metric_name(check['name'])
        # Latest per-check state for the UI: {name: (up: bool, latency_ms or None)}
        self._states: Dict[str, tuple] = {}

    async def on_collect(self):
        if not self.checks:
            self.db_logger.write("No checks configured", level="WARNING")
            self.set_status('offline')
            return

        script = _build_probe_script(self.checks, self.timeout)
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Probe script failed to run: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        results = _parse_results(stdout, len(self.checks))
        down: List[str] = []
        for i, check in enumerate(self.checks):
            latency = results.get(i)
            up = latency is not None
            self._states[check['name']] = (up, latency)
            # 1 = reachable, 0 = down — always recorded so the boolean has history.
            self.db_metrics.metric(f"{check['metric']}_up", 1.0 if up else 0.0)
            if up:
                self.db_metrics.metric(f"{check['metric']}_latency_ms", latency)
            else:
                down.append(check['name'])

        if down:
            self.db_logger.write(f"{len(down)} check(s) down: {', '.join(down)}", level="ERROR")
            self.set_status('failed')
        else:
            self.db_logger.write(f"All {len(self.checks)} check(s) reachable", level="INFO")
            self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('up_card'):
            up_label = info_card('REACHABLE', '--')
        with layout.cell('down_card'):
            down_label = info_card('DOWN', '--')
        with layout.cell('charts'):
            for check in self.checks:
                history_chart(f"{check['name']} LATENCY (ms)", self.id, f"{check['metric']}_latency_ms")
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            if not self._states:
                return
            up = sum(1 for u, _ in self._states.values() if u)
            down = len(self._states) - up
            up_label.text = f'{up}/{len(self._states)}'
            down_label.text = f'{down}'
            down_label.style(f'color: {STATUS_COLORS["failed" if down else "online"]}')

        safe_timer(5.0, update_cards)
