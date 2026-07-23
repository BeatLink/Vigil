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

Control actions:

  Enable Blocking    Turns blocking back on with no timer, for the "blocking is
                      DISABLED" fault this monitor flags. Safe to fire any time
                      blocking is off, whether that was deliberate or not — it
                      is not offered when blocking already reads enabled.
  Update Gravity      Rebuilds the blocklist database, the remediation for both
                      an empty gravity list and a stale one. Run on demand
                      rather than on a timer here: an automatic rebuild retry
                      would mask a blocklist source that is failing every time.

Disabling blocking is deliberately not offered as a button: it is the one state
this monitor exists to catch, so a dashboard control for it would let a
mis-click quietly recreate the fault it watches for.
"""
import json
import shlex
from typing import Any, Dict, List, Optional, Tuple

from vigil.core.common.time_utils import parse_duration
from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# Marks the end of the summary payload so both API responses can be fetched in
# one SSH round trip and split apart again. A newline-free sentinel that cannot
# occur in JSON.
_SEP = "@@VIGIL_SPLIT@@"


def _auth_preamble(base: str, timeout: int, password_command: Optional[str],
                   password: Optional[str]) -> Tuple[List[str], str]:
    """
    Build the shell lines that exchange a password for a session id, and the
    curl flags that carry it.

    Returns (lines, auth_flags). When no credentials are configured the lines
    are empty and the flags blank, matching an instance reachable without auth.
    The password is resolved on the monitored host when `password_command` is
    used, so it never passes through Vigil.
    """
    if not (password_command or password):
        return [], ''

    lines = []
    if password_command:
        lines.append(f"__pw=$({password_command})")
    else:
        lines.append(f"__pw={shlex.quote(password)}")

    # Exchange the password for a session id. jq is not assumed present, so
    # the sid is pulled out with sed.
    lines.append(
        f'__sid=$(curl -s -m {timeout} -X POST {shlex.quote(base + "/api/auth")} '
        f'-H "Content-Type: application/json" '
        f'''--data "{{\\"password\\":\\"$__pw\\"}}" '''
        f"""| sed -n 's/.*"sid"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')"""
    )
    return lines, '-H "X-FTL-SID: $__sid"'


def _build_fetch_script(api_url: str, timeout: int, password_command: Optional[str],
                        password: Optional[str]) -> str:
    """
    Build a shell script that fetches the summary and blocking-status endpoints.

    Authenticates first when a password is supplied, reusing the returned
    session id (SID) for both calls.
    """
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
    """
    Build a shell script that POSTs a new blocking state.

    `--fail` turns a rejected request (e.g. an expired session) into a non-zero
    exit rather than a "successful" curl that merely fetched an error body.
    """
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
    """
    Build a shell script that triggers a gravity (blocklist) rebuild.

    `/api/action/gravity` streams progress as Server-Sent Events rather than
    returning a single JSON body; the stream is discarded and only the exit
    code is used; a generous timeout is used since a full rebuild can take
    much longer than the usual API round trip.
    """
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
    ['events'],
]


class PiholeCollectorPlugin(CollectorPlugin):
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
        # Gravity rebuilds take far longer than a status read, so they get
        # their own generous deadline rather than inheriting api_timeout.
        self.gravity_timeout = int(config.get('gravity_timeout', 120))

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

    def get_actions(self) -> List[Dict[str, str]]:
        """
        Expose the remediations for the faults this monitor detects.

        Disabling blocking is deliberately not offered: it is the one state
        this monitor exists to catch, so a dashboard control for it would let
        a mis-click quietly recreate the fault it watches for. Enable and
        gravity-rebuild are both reversible, queue-free operations, matching
        the "no confirmation step" constraint the dashboard fires actions
        under.
        """
        return [
            {'name': 'Enable Blocking', 'action_id': 'enable_blocking',
             'variant': 'primary', 'icon': 'shield'},
            {'name': 'Update Gravity', 'action_id': 'update_gravity',
             'variant': 'secondary', 'icon': 'refresh'},
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id == 'enable_blocking':
            script = _build_blocking_script(
                self.api_url, self.api_timeout, self.api_password_command,
                self.api_password, enabled=True,
            )
            status, _, stderr = await self.ssh_controller.execute_action(script)
            if status != 0:
                self.db_logger.write(
                    f"Failed to enable blocking: {stderr.strip()}", level="ERROR")
                return False
            self.db_logger.write("Blocking enabled", level="INFO")
            return True

        if action_id == 'update_gravity':
            script = _build_gravity_script(
                self.api_url, self.gravity_timeout, self.api_password_command,
                self.api_password,
            )
            status, _, stderr = await self.ssh_controller.execute_action(
                script, timeout=self.gravity_timeout)
            if status != 0:
                self.db_logger.write(
                    f"Gravity update failed: {stderr.strip()}", level="ERROR")
                return False
            self.db_logger.write("Gravity update triggered", level="INFO")
            return True

        return False


class PiholeUIPlugin(UIPlugin):
    """Dashboard rendering for the pihole monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        block_rate_warning = float(self.config.get('block_rate_warning', 5))
        block_rate_threshold = float(self.config.get('block_rate_threshold', 1))
        gravity_max_age = parse_duration(self.config.get('gravity_max_age', '8d'))

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
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            block_rate = self.latest_metric('block_rate_pct')
            total      = self.latest_metric('queries_total')
            domains    = self.latest_metric('gravity_domains')
            age        = self.latest_metric('gravity_age_seconds')
            active     = self.latest_metric('clients_active')
            enabled    = self.latest_metric('blocking_enabled')

            if block_rate:
                block_rate_label.text = f'{block_rate.value:.1f}%'
                if block_rate.value < block_rate_threshold:
                    colour = STATUS_COLORS['failed']
                elif block_rate.value < block_rate_warning:
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
                        f'color: {STATUS_COLORS["warning" if age.value > gravity_max_age else "online"]}'
                    )
            if active:
                clients_label.text = f'{int(active.value)}'
            if enabled:
                on = enabled.value >= 1.0
                blocking_label.text = 'ENABLED' if on else 'DISABLED'
                blocking_label.style(f'color: {STATUS_COLORS["online" if on else "failed"]}')

        on_data_event('metric', block_rate_label, update_cards)
