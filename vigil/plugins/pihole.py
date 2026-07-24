import json
import shlex
from typing import Any, Dict, List, Optional, Tuple, Union

from vigil.core.common.time_utils import parse_duration
from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

_SEP = "@@VIGIL_SPLIT@@"


def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   password: Optional[str]) -> Tuple[List[str], str]:
    if not (password_command or password):
        return [], ''

    lines = []
    if password_command:
        lines.append(f"__pw=$({password_command})")
    else:
        lines.append(f"__pw={shlex.quote(password)}")

    lines.append(
        f'__sid=$(curl -s -m {timeout} -X POST {shlex.quote(base + "/api/auth")} '
        f'-H "Content-Type: application/json" '
        f'''--data "{{\\"password\\":\\"$__pw\\"}}" '''
        f"""| sed -n 's/.*"sid"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')"""
    )
    return lines, '-H "X-FTL-SID: $__sid"'


def _build_fetch_script(api_url: str, timeout: int, password_command: Optional[str],
                        password: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    auth_lines, auth = _auth_preamble(base, timeout, password_command, password)
    lines.extend(auth_lines)

    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/stats/summary")}')
    lines.append(f'echo "{_SEP}"')
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
    if _SEP not in stdout:
        raise ValueError(f"unexpected API response: {stdout[:200]!r}")
    summary_raw, blocking_raw = stdout.split(_SEP, 1)
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


def _format_age(seconds: float) -> str:
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


class PiholeCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:80')
        self.api_password = config.get('api_password')
        self.api_password_command = config.get('api_password_command')
        self.block_rate_warning = float(config.get('block_rate_warning', 5))
        self.block_rate_threshold = float(config.get('block_rate_threshold', 1))
        self.gravity_max_age = parse_duration(config.get('gravity_max_age', '8d'))
        self.min_queries = int(config.get('min_queries', 100))
        self.api_timeout = int(config.get('api_timeout', 10))
        self.gravity_timeout = int(config.get('gravity_timeout', 120))

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout,
            self.api_password_command, self.api_password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Pi-hole API: {stderr.strip()}")

        try:
            summary, blocking = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        queries = summary.get('queries', {})
        gravity = summary.get('gravity', {})
        clients = summary.get('clients', {})

        total = float(queries.get('total', 0) or 0)
        blocked = float(queries.get('blocked', 0) or 0)
        block_rate = queries.get('percent_blocked')
        if block_rate is None:
            block_rate = (100.0 * blocked / total) if total else 0.0
        block_rate = float(block_rate)

        domains_blocked = float(gravity.get('domains_being_blocked', 0) or 0)
        blocking_enabled = blocking.get('blocking') == 'enabled'

        metrics = {
            'block_rate_pct': block_rate,
            'queries_total': total,
            'queries_blocked': blocked,
            'queries_forwarded': float(queries.get('forwarded', 0) or 0),
            'queries_cached': float(queries.get('cached', 0) or 0),
            'unique_domains': float(queries.get('unique_domains', 0) or 0),
            'gravity_domains': domains_blocked,
            'clients_active': float(clients.get('active', 0) or 0),
            'blocking_enabled': 1.0 if blocking_enabled else 0.0,
        }

        gravity_age: Optional[float] = None
        last_update = gravity.get('last_update')
        if last_update:
            import time as _time
            gravity_age = max(0.0, _time.time() - float(last_update))
            metrics['gravity_age_seconds'] = gravity_age

        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if not blocking_enabled:
            problems.append("blocking is DISABLED")
            _escalate('failed')

        if domains_blocked <= 0:
            problems.append("gravity list is empty")
            _escalate('failed')

        if total >= self.min_queries:
            if block_rate < self.block_rate_threshold:
                problems.append(
                    f"block rate {block_rate:.1f}% below {self.block_rate_threshold}%"
                )
                _escalate('failed')
            elif block_rate < self.block_rate_warning:
                problems.append(
                    f"block rate {block_rate:.1f}% below {self.block_rate_warning}%"
                )
                _escalate('warning')

        if gravity_age is None:
            problems.append("gravity has never been updated")
            _escalate('warning')
        elif gravity_age > self.gravity_max_age:
            problems.append(f"gravity list is {_format_age(gravity_age)} old")
            _escalate('warning')

        parts = [
            f"{block_rate:.1f}% blocked",
            f"{int(total):,} queries",
            f"{int(domains_blocked):,} domains on list",
            f"{int(clients.get('active', 0) or 0)} active clients",
        ]
        if gravity_age is not None:
            parts.append(f"list {_format_age(gravity_age)} old")
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        return CollectResult(
            metrics=metrics,
            logs=[(' | '.join(parts), log_level)],
            status=level,
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


class PiholeUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.block_rate_warning = float(config.get('block_rate_warning', 5))
        self.block_rate_threshold = float(config.get('block_rate_threshold', 1))
        self.gravity_max_age = parse_duration(config.get('gravity_max_age', '8d'))

        from vigil.web.ui.spec import register_color_rule
        self._block_rate_rule_name = f'pihole_block_rate_{self.id}'

        @register_color_rule(self._block_rate_rule_name)
        def _block_rate_color(v, _warning=self.block_rate_warning, _threshold=self.block_rate_threshold):
            if v is None:
                return None
            if v < _threshold:
                return 'failed'
            if v < _warning:
                return 'warning'
            return 'online'

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.spec import FORMATTERS, COLOR_RULES
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )
        page = self.ui.page(metric_names=[
            'block_rate_pct', 'queries_total', 'gravity_domains',
            'gravity_age_seconds', 'clients_active', 'blocking_enabled',
        ])

        pct_formatter = FORMATTERS['percent1']
        count_formatter = FORMATTERS['count_comma']
        int_formatter = FORMATTERS['int']
        block_rate_rule = COLOR_RULES[self._block_rate_rule_name]

        def _enabled_text(v):
            if v is None:
                return '--'
            return 'ENABLED' if v >= 1.0 else 'DISABLED'

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('block_rate_card'):
            block_rate_label = info_card('BLOCK RATE', pct_formatter(None)).bind_text_from(
                page.model, ('metrics', 'block_rate_pct'), backward=pct_formatter)
        with layout.cell('queries_card'):
            info_card('QUERIES', count_formatter(None)).bind_text_from(
                page.model, ('metrics', 'queries_total'), backward=count_formatter)
        with layout.cell('gravity_card'):
            gravity_label = info_card('BLOCKLIST', '--')
        with layout.cell('clients_card'):
            info_card('ACTIVE CLIENTS', int_formatter(None)).bind_text_from(
                page.model, ('metrics', 'clients_active'), backward=int_formatter)
        with layout.cell('blocking_card'):
            blocking_label = info_card('BLOCKING', '--').bind_text_from(
                page.model, ('metrics', 'blocking_enabled'), backward=_enabled_text)
        with layout.cell('chart'):
            history_chart(page, 'BLOCK RATE (%)', self.id, 'block_rate_pct')
        with layout.cell('events'):
            self.ui.events_table(page)

        def update_colors():
            block_rate = page.model.metrics.get('block_rate_pct')
            if block_rate is not None:
                state = block_rate_rule(block_rate)
                if state is not None:
                    block_rate_label.style(f'color: {STATUS_COLORS[state]}')

            domains = page.model.metrics.get('gravity_domains')
            age = page.model.metrics.get('gravity_age_seconds')
            if domains is not None:
                text = f'{int(domains):,} domains'
                if age is not None:
                    text += f' ({_format_age(age)} old)'
                    gravity_label.style(
                        f'color: {STATUS_COLORS["warning" if age > self.gravity_max_age else "online"]}'
                    )
                gravity_label.text = text

            enabled = page.model.metrics.get('blocking_enabled')
            if enabled is not None:
                on = enabled >= 1.0
                blocking_label.style(f'color: {STATUS_COLORS["online" if on else "failed"]}')

        page.on_refresh(update_colors)
        page.start()
