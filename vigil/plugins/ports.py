from typing import Dict, Any, List, Optional, Tuple

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_FAIL = "FAIL"


def _safe_metric_name(label: str) -> str:
    return ''.join(c if c.isalnum() else '_' for c in label.lower()).strip('_') or 'check'


def _build_probe_script(checks: List[Dict[str, Any]], timeout: int) -> str:
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


def _named_checks(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    named = []
    for check in checks:
        c = dict(check)
        if 'name' not in c:
            c['name'] = c['url'] if c.get('url') else f"{c.get('host', 'localhost')}:{c.get('port')}"
        c['metric'] = _safe_metric_name(c['name'])
        named.append(c)
    return named


_DEFAULT_LAYOUT = [
    ['host_card', 'up_card', 'down_card'],
    ['charts'],
    ['events'],
]


class PortsCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.timeout = int(config.get('timeout', 5))
        self.checks: List[Dict[str, Any]] = config.get('checks', [])
        for check in self.checks:
            if 'name' not in check:
                check['name'] = check['url'] if check.get('url') else f"{check.get('host', 'localhost')}:{check.get('port')}"
            check['metric'] = _safe_metric_name(check['name'])

    def commands(self) -> List[Command]:
        if not self.checks:
            return []
        return [Command(_build_probe_script(self.checks, self.timeout))]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not self.checks:
            return CollectResult.failed("No checks configured", level="WARNING", status='offline')

        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Probe script failed to run: {stderr}")

        parsed = _parse_results(stdout, len(self.checks))
        metrics: Dict[str, float] = {}
        down: List[str] = []
        for i, check in enumerate(self.checks):
            latency = parsed.get(i)
            up = latency is not None
            metrics[f"{check['metric']}_up"] = 1.0 if up else 0.0
            if up:
                metrics[f"{check['metric']}_latency_ms"] = latency
            else:
                down.append(check['name'])

        if down:
            return CollectResult(
                metrics=metrics,
                logs=[(f"{len(down)} check(s) down: {', '.join(down)}", "ERROR")],
                status='failed',
            )
        return CollectResult(
            metrics=metrics,
            logs=[(f"All {len(self.checks)} check(s) reachable", "INFO")],
            status='online',
        )


class PortsUIPlugin(UIPlugin):
    @property
    def _checks(self) -> List[Dict[str, Any]]:
        return _named_checks(self.config.get('checks', []))

    def _check_counts(self) -> Optional[Tuple[int, int]]:
        checks = self._checks
        if not checks:
            return None
        up = down = 0
        for check in checks:
            m = self.storage.latest_metric(f"{check['metric']}_up")
            if m is None:
                continue
            if m.value >= 1.0:
                up += 1
            else:
                down += 1
        return (up, down) if up + down else None

    @property
    def _up_text(self) -> str:
        counts = self._check_counts()
        if counts is None:
            return '--'
        up, down = counts
        return f'{up}/{up + down}'

    @property
    def _down_text(self) -> str:
        counts = self._check_counts()
        return '--' if counts is None else str(counts[1])

    @property
    def _down_color(self) -> Optional[str]:
        counts = self._check_counts()
        if counts is None:
            return None
        return 'failed' if counts[1] else 'online'

    @property
    def _chart_items(self) -> List[Tuple[str, str]]:
        return [(f"{check['name']} LATENCY (ms)", f"{check['metric']}_latency_ms") for check in self._checks]

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'up_card': {'title': 'REACHABLE', 'value_attr': '_up_text', 'refresh': True},
                'down_card': {'title': 'DOWN', 'value_attr': '_down_text', 'color_attr': '_down_color'},
            },
            'dynamic_charts': {'widget': 'charts', 'items_attr': '_chart_items'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)
