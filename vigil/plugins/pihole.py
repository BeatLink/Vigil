"""Pi-hole effectiveness via the FTL v6 API, fetched by one curl script over
SSH — sampled locally by the agent on agent-backed hosts — that authenticates
for a session, reads /api/stats/summary and /api/dns/blocking, and releases
the session on exit. Config: api_url, api_password / api_password_command,
block_rate_warning, block_rate_threshold, gravity_max_age, min_queries,
api_timeout, gravity_timeout. Disabled blocking, an empty gravity list, or a
block rate under block_rate_threshold (once min_queries is reached) is
failed; a merely low block rate or a stale gravity list is warning."""

import json
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from vigil.plugins.base.plugin_helpers import (
    SCRIPT_SEP, StatusAccumulator, dq, parse_duration, password_line,
)
from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import ActionPlan, CmdResult, Command, CollectResult

def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   password: Optional[str]) -> Tuple[List[str], str]:
    if not (password_command or password):
        return [], ''

    lines = [password_line(password_command, password)]
    lines.append(
        f'__sid=$(curl -s -m {timeout} -X POST {shlex.quote(base + "/api/auth")} '
        f'-H "Content-Type: application/json" '
        f'''--data "{{\\"password\\":\\"$__pw\\"}}" '''
        f"""| sed -n 's/.*"sid"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')"""
    )
    # Release the session however the script exits: FTL's session table is
    # finite, and an unreleased sid per poll can crowd out real logins.
    lines.append(
        "trap '[ -n \"$__sid\" ] && curl -s -m %d -X DELETE "
        '-H \"X-FTL-SID: $__sid\" %s >/dev/null 2>&1 || true\' EXIT'
        % (timeout, dq(base + "/api/auth"))
    )
    return lines, '-H "X-FTL-SID: $__sid"'


def _build_fetch_script(api_url: str, timeout: int, password_command: Optional[str],
                        password: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, password)
    lines.extend(auth_lines)

    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/stats/summary")}')
    lines.append(f'echo "{SCRIPT_SEP}"')
    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/dns/blocking")}')
    return '\n'.join(lines)


def _build_blocking_script(api_url: str, timeout: int, password_command: Optional[str],
                           password: Optional[str], enabled: bool) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, password)
    lines.extend(auth_lines)

    body = json.dumps({"blocking": enabled, "timer": None})
    lines.append(
        f'curl -s -f -m {timeout} -X POST {auth} '
        f'-H "Content-Type: application/json" '
        f'--data {shlex.quote(body)} '
        f'{shlex.quote(base + "/api/dns/blocking")}'
    )
    return '\n'.join(lines)


def _build_gravity_script(api_url: str, timeout: int, password_command: Optional[str],
                          password: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, password)
    lines.extend(auth_lines)

    lines.append(
        f'curl -s -f -m {timeout} {auth} '
        f'{shlex.quote(base + "/api/action/gravity")} > /dev/null'
    )
    return '\n'.join(lines)


def _parse_response(stdout: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if SCRIPT_SEP not in stdout:
        raise ValueError(f"unexpected API response: {stdout[:200]!r}")
    summary_raw, blocking_raw = stdout.split(SCRIPT_SEP, 1)
    try:
        summary = json.loads(summary_raw.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"summary was not JSON ({e}): {summary_raw[:200]!r}") from e
    try:
        blocking = json.loads(blocking_raw.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"blocking status was not JSON ({e}): {blocking_raw[:200]!r}") from e

    if 'queries' not in summary:
        if isinstance(summary.get('error'), dict):
            msg = summary['error'].get('message', 'unknown error')
            raise ValueError(f"API returned an error: {msg} (set api_password if auth is required)")
        raise ValueError(f"summary missing 'queries': {summary_raw[:200]!r}")

    return summary, blocking


def _collect_metrics(summary: Dict[str, Any], blocking: Dict[str, Any]) -> Dict[str, float]:
    """Flatten the summary and blocking payloads into the metric dict."""
    queries = summary.get('queries', {})
    gravity = summary.get('gravity', {})
    clients = summary.get('clients', {})
    return {
        'block_rate_pct': _block_rate(queries),
        'queries_total': float(queries.get('total', 0) or 0),
        'queries_blocked': float(queries.get('blocked', 0) or 0),
        'queries_forwarded': float(queries.get('forwarded', 0) or 0),
        'queries_cached': float(queries.get('cached', 0) or 0),
        'unique_domains': float(queries.get('unique_domains', 0) or 0),
        'gravity_domains': float(gravity.get('domains_being_blocked', 0) or 0),
        'clients_active': float(clients.get('active', 0) or 0),
        'blocking_enabled': 1.0 if blocking.get('blocking') == 'enabled' else 0.0,
    }


def _block_rate(queries: Dict[str, Any]) -> float:
    """The blocked-query percentage, computed from the totals when the API omits it."""
    rate = queries.get('percent_blocked')
    if rate is None:
        total = float(queries.get('total', 0) or 0)
        blocked = float(queries.get('blocked', 0) or 0)
        rate = (100.0 * blocked / total) if total else 0.0
    return float(rate)


def _gravity_age_seconds(gravity: Dict[str, Any], now: float) -> Optional[float]:
    """Seconds since the last gravity update, or None if it never ran."""
    last_update = gravity.get('last_update')
    if not last_update:
        return None
    return max(0.0, now - float(last_update))


def _evaluate_health(metrics: Dict[str, float], gravity_age: Optional[float],
                     block_rate_warning: float, block_rate_threshold: float,
                     gravity_max_age: int, min_queries: int) -> StatusAccumulator:
    """Judge blocking state, block rate, and gravity freshness against the thresholds."""
    acc = StatusAccumulator()
    block_rate = metrics['block_rate_pct']

    if metrics['blocking_enabled'] != 1.0:
        acc.escalate('failed', "blocking is DISABLED")

    if metrics['gravity_domains'] <= 0:
        acc.escalate('failed', "gravity list is empty")

    if metrics['queries_total'] >= min_queries:
        if block_rate < block_rate_threshold:
            acc.escalate('failed',
                         f"block rate {block_rate:.1f}% below {block_rate_threshold}%")
        elif block_rate < block_rate_warning:
            acc.escalate('warning',
                         f"block rate {block_rate:.1f}% below {block_rate_warning}%")

    if gravity_age is None:
        acc.escalate('warning', "gravity has never been updated")
    elif gravity_age > gravity_max_age:
        acc.escalate('warning', f"gravity list is {_format_age_compact(gravity_age)} old")
    return acc


def _format_age_compact(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    if days:
        return f"{days}d {hours}h"
    minutes = (rem % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_DEFAULT_LAYOUT = [
    ['host_card', 'block_rate_card', 'queries_card'],
    ['gravity_card', 'clients_card', 'blocking_card'],
    ['chart'],
    ['events'],
]


class Pihole(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.api_url = config.get('api_url', 'http://127.0.0.1:80')
        self.api_password = config.get('api_password')
        self.api_password_command = config.get('api_password_command')
        self.block_rate_warning = float(config.get('block_rate_warning', 5))
        self.block_rate_threshold = float(config.get('block_rate_threshold', 1))
        self.gravity_max_age = parse_duration(config.get('gravity_max_age', '8d'))
        self.min_queries = int(config.get('min_queries', 100))
        self.api_timeout = int(config.get('api_timeout', 10))
        self.gravity_timeout = int(config.get('gravity_timeout', 120))

        self._blocking_format = (
            lambda v: '--' if v is None else ('ENABLED' if v >= 1.0 else 'DISABLED'))
        self._blocking_color = (
            lambda v: None if v is None else ('online' if v >= 1.0 else 'failed'))

    SAMPLED = True

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout,
            self.api_password_command, self.api_password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Turns the summary+blocking curl output into a CollectResult with
        query/gravity/client metrics, one summary log line, and a status where
        disabled blocking, an empty gravity list, or a block rate under the
        threshold is failed and a low rate or stale gravity is warning."""
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Pi-hole API: {stderr.strip()}")

        try:
            summary, blocking = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        metrics = _collect_metrics(summary, blocking)
        gravity_age = _gravity_age_seconds(summary.get('gravity', {}), time.time())
        if gravity_age is not None:
            metrics['gravity_age_seconds'] = gravity_age

        acc = _evaluate_health(metrics, gravity_age,
                               self.block_rate_warning, self.block_rate_threshold,
                               self.gravity_max_age, self.min_queries)

        parts = [
            f"{metrics['block_rate_pct']:.1f}% blocked",
            f"{int(metrics['queries_total']):,} queries",
            f"{int(metrics['gravity_domains']):,} domains on list",
            f"{int(metrics['clients_active'])} active clients",
        ]
        if gravity_age is not None:
            parts.append(f"list {_format_age_compact(gravity_age)} old")
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(
            metrics=metrics,
            logs=[(' | '.join(parts), acc.log_level)],
            status=acc.status,
        )

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Enable Blocking', 'action_id': 'enable_blocking',
             'variant': 'primary', 'icon': 'shield'},
            {'name': 'Update Gravity', 'action_id': 'update_gravity',
             'variant': 'secondary', 'icon': 'refresh'},
        ]

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        if action_id == 'enable_blocking':
            script = _build_blocking_script(
                self.api_url, self.api_timeout, self.api_password_command,
                self.api_password, enabled=True,
            )
            return ActionPlan(script)

        if action_id == 'update_gravity':
            script = _build_gravity_script(
                self.api_url, self.gravity_timeout, self.api_password_command,
                self.api_password,
            )
            return ActionPlan(script, timeout=self.gravity_timeout)

        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id == 'enable_blocking':
            if result.exit_code != 0:
                return CollectResult.failed(f"Failed to enable blocking: {result.stderr.strip()}")
            return CollectResult(logs=[("Blocking enabled", "INFO")], success=True)

        if action_id == 'update_gravity':
            if result.exit_code != 0:
                return CollectResult.failed(f"Gravity update failed: {result.stderr.strip()}")
            return CollectResult(logs=[("Gravity update triggered", "INFO")], success=True)

        return result.exit_code == 0

    def _block_rate_color(self, v: Optional[float]) -> Optional[str]:
        if v is None:
            return None
        if v < self.block_rate_threshold:
            return 'failed'
        if v < self.block_rate_warning:
            return 'warning'
        return 'online'

    def _gravity_text(self, values: Dict[str, Any]) -> str:
        domains = values.get('gravity_domains')
        if domains is None:
            return '--'
        text = f'{int(domains):,} domains'
        age = values.get('gravity_age_seconds')
        if age is not None:
            text += f' ({_format_age_compact(age)} old)'
        return text

    def _gravity_color(self, values: Dict[str, Any]) -> Optional[str]:
        age = values.get('gravity_age_seconds')
        if age is None:
            return None
        return 'warning' if age > self.gravity_max_age else 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'block_rate_card': {'metric': 'block_rate_pct', 'title': 'BLOCK RATE', 'format': 'percent1',
                                    'color': self._block_rate_color},
                'queries_card': {'metric': 'queries_total', 'title': 'QUERIES', 'format': 'count_comma'},
                'gravity_card': {'title': 'BLOCKLIST', 'metrics': ['gravity_domains', 'gravity_age_seconds'],
                                 'format_fn': self._gravity_text, 'color_fn': self._gravity_color},
                'clients_card': {'metric': 'clients_active', 'title': 'ACTIVE CLIENTS', 'format': 'int'},
                'blocking_card': {'metric': 'blocking_enabled', 'title': 'BLOCKING',
                                  'format': self._blocking_format, 'color': self._blocking_color},
            },
            'chart': {'metric': 'block_rate_pct', 'title': 'BLOCK RATE (%)'},
            'events': True,
        }

