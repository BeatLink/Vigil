"""
Pi-hole DNS statistics via the v6 REST API.

Complements a `systemd_service` monitor on pihole-ftl rather than replacing it.
That one answers "is the process alive"; this one answers "is it still doing its
job", which is a different failure. The case that motivates it: gravity (the
blocklist database) fails to rebuild, so Pi-hole keeps resolving happily while
blocking nothing. Every liveness check stays green through that — the service is
up, the web UI answers, the port is open — and the only visible symptom is a
block rate that quietly fell to zero.

Two signals carry that:

  block rate      Share of queries blocked. A collapse toward zero means gravity
                  is loaded but matching nothing.
  gravity age     Time since the blocklist last rebuilt. Catches the same fault
                  earlier, before enough queries accumulate to move the rate.

Both are checked because they fail on different clocks: a stale list that still
matches keeps the rate healthy, and a freshly rebuilt but empty list keeps the
age healthy. Neither alone is sufficient.

The API is read over SSH with curl rather than from Vigil's own process: FTL
binds its API to the Pi-hole host's loopback, so it is not reachable across the
network. This matches how `ports` probes from the remote host.

Pi-hole 6 replaced the old `admin/api.php` with `/api/*` and normally requires a
session token. Where the API is reachable without one (the default for local
requests), no credentials are needed; set `api_password` if the instance
requires authentication.

Config options:
  api_url             Base URL of the Pi-hole API, as seen from the monitored
                      host (default: http://127.0.0.1:80)
  api_password        App password, if the instance requires authentication.
                      Prefer api_password_command over inlining a secret here.
  api_password_command
                      Command run on the monitored host whose stdout is the
                      password (e.g. "cat /run/secrets/pihole_api").
  block_rate_warning  Block % below which status is warning (default: 5)
  block_rate_threshold
                      Block % below which status is failed  (default: 1)
  gravity_max_age     Blocklist age past which status is warning (default: 8d)
  min_queries         Queries needed before the block rate is judged at all
                      (default: 100). Below this the ratio is noise — a freshly
                      restarted FTL legitimately shows 0% after two queries.
"""
import json
import shlex
from typing import Any, Dict, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# Marks the end of the summary payload so both API responses can be fetched in
# one SSH round trip and split apart again. A newline-free sentinel that cannot
# occur in JSON.
_SEP = "@@VIGIL_SPLIT@@"


def _build_fetch_script(api_url: str, timeout: int, password_command: Optional[str],
                        password: Optional[str]) -> str:
    """
    Build a shell script that fetches the summary and blocking-status endpoints.

    Authenticates first when a password is supplied, reusing the returned
    session id (SID) for both calls. The password is resolved on the remote host
    when `password_command` is used, so it never passes through Vigil.
    """
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if password_command:
        lines.append(f"__pw=$({password_command})")
    elif password:
        lines.append(f"__pw={shlex.quote(password)}")

    if password_command or password:
        # Exchange the password for a session id. jq is not assumed present, so
        # the sid is pulled out with sed.
        lines.append(
            f'__sid=$(curl -s -m {timeout} -X POST {shlex.quote(base + "/api/auth")} '
            f'-H "Content-Type: application/json" '
            f'''--data "{{\\"password\\":\\"$__pw\\"}}" '''
            f"""| sed -n 's/.*"sid"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')"""
        )
        auth = '-H "X-FTL-SID: $__sid"'
    else:
        auth = ''

    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/stats/summary")}')
    lines.append(f'echo "{_SEP}"')
    lines.append(f'curl -s -m {timeout} {auth} {shlex.quote(base + "/api/dns/blocking")}')
    return '\n'.join(lines)


def _parse_response(stdout: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Split and parse the two JSON payloads.

    Raises ValueError with a readable message on anything unparseable, so the
    caller can log one cause rather than a bare KeyError.
    """
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

    # An authenticated instance answers 401 with a JSON error body rather than
    # the expected shape; say so plainly instead of reporting a missing key.
    if 'queries' not in summary:
        if isinstance(summary.get('error'), dict):
            msg = summary['error'].get('message', 'unknown error')
            raise ValueError(f"API returned an error: {msg} (set api_password if auth is required)")
        raise ValueError(f"summary missing 'queries': {summary_raw[:200]!r}")

    return summary, blocking


def _format_age(seconds: float) -> str:
    """Format a duration as a compact human-readable string."""
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
    ['logs'],
]


class PiholePlugin(BasePlugin):
    """Monitors Pi-hole DNS filtering health via the v6 REST API."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:80')
        self.api_password = config.get('api_password')
        self.api_password_command = config.get('api_password_command')
        self.block_rate_warning = float(config.get('block_rate_warning', 5))
        self.block_rate_threshold = float(config.get('block_rate_threshold', 1))
        self.gravity_max_age = parse_duration(config.get('gravity_max_age', '8d'))
        self.min_queries = int(config.get('min_queries', 100))
        # Seconds allowed for the remote curl calls. Distinct from self.timeout,
        # which bounds the SSH command as a whole and must stay the larger.
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        script = _build_fetch_script(
            self.api_url, self.api_timeout,
            self.api_password_command, self.api_password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Pi-hole API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            summary, blocking = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        queries = summary.get('queries', {})
        gravity = summary.get('gravity', {})
        clients = summary.get('clients', {})

        total = float(queries.get('total', 0) or 0)
        blocked = float(queries.get('blocked', 0) or 0)
        # Prefer FTL's own percentage; fall back to computing it when absent.
        block_rate = queries.get('percent_blocked')
        if block_rate is None:
            block_rate = (100.0 * blocked / total) if total else 0.0
        block_rate = float(block_rate)

        domains_blocked = float(gravity.get('domains_being_blocked', 0) or 0)
        blocking_enabled = blocking.get('blocking') == 'enabled'

        self.db_metrics.metric('block_rate_pct', block_rate)
        self.db_metrics.metric('queries_total', total)
        self.db_metrics.metric('queries_blocked', blocked)
        self.db_metrics.metric('queries_forwarded', float(queries.get('forwarded', 0) or 0))
        self.db_metrics.metric('queries_cached', float(queries.get('cached', 0) or 0))
        self.db_metrics.metric('unique_domains', float(queries.get('unique_domains', 0) or 0))
        self.db_metrics.metric('gravity_domains', domains_blocked)
        self.db_metrics.metric('clients_active', float(clients.get('active', 0) or 0))
        self.db_metrics.metric('blocking_enabled', 1.0 if blocking_enabled else 0.0)

        # Gravity age, measured against the monitored host's clock via the
        # API's own timestamp. Absent on an instance that has never built a
        # list, which is itself worth reporting.
        gravity_age: Optional[float] = None
        last_update = gravity.get('last_update')
        if last_update:
            import time as _time
            gravity_age = max(0.0, _time.time() - float(last_update))
            self.db_metrics.metric('gravity_age_seconds', gravity_age)

        # --- status ---------------------------------------------------------
        # Each condition is judged independently and the worst one wins, so a
        # healthy block rate cannot mask a stale list (or vice versa).
        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if not blocking_enabled:
            # Deliberate but easy to forget: blocking disabled from the UI has
            # no timer set here, so it stays off until someone notices.
            problems.append("blocking is DISABLED")
            _escalate('failed')

        if domains_blocked <= 0:
            problems.append("gravity list is empty")
            _escalate('failed')

        # Below min_queries the ratio is dominated by whatever handful of
        # lookups happened to arrive, so it is not evidence of anything.
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
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('block_rate_card'):
            block_rate_label = info_card('BLOCK RATE', '-- %')
        with layout.cell('queries_card'):
            queries_label = info_card('QUERIES', '--')
        with layout.cell('gravity_card'):
            gravity_label = info_card('BLOCKLIST', '--')
        with layout.cell('clients_card'):
            clients_label = info_card('ACTIVE CLIENTS', '--')
        with layout.cell('blocking_card'):
            blocking_label = info_card('BLOCKING', '--')
        with layout.cell('chart'):
            history_chart('BLOCK RATE (%)', self.id, 'block_rate_pct')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            block_rate = self.latest_metric('block_rate_pct')
            total      = self.latest_metric('queries_total')
            domains    = self.latest_metric('gravity_domains')
            age        = self.latest_metric('gravity_age_seconds')
            active     = self.latest_metric('clients_active')
            enabled    = self.latest_metric('blocking_enabled')

            if block_rate:
                block_rate_label.text = f'{block_rate.value:.1f}%'
                if block_rate.value < self.block_rate_threshold:
                    colour = STATUS_COLORS['failed']
                elif block_rate.value < self.block_rate_warning:
                    colour = STATUS_COLORS['warning']
                else:
                    colour = STATUS_COLORS['online']
                block_rate_label.style(f'color: {colour}')
            if total:
                queries_label.text = f'{int(total.value):,}'
            if domains:
                gravity_label.text = f'{int(domains.value):,} domains'
                if age:
                    gravity_label.text += f' ({_format_age(age.value)} old)'
                    gravity_label.style(
                        f'color: {STATUS_COLORS["warning" if age.value > self.gravity_max_age else "online"]}'
                    )
            if active:
                clients_label.text = f'{int(active.value)}'
            if enabled:
                on = enabled.value >= 1.0
                blocking_label.text = 'ENABLED' if on else 'DISABLED'
                blocking_label.style(f'color: {STATUS_COLORS["online" if on else "failed"]}')

        safe_timer(5.0, update_cards)
