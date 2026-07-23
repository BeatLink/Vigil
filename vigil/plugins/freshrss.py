"""
FreshRSS feed-refresh staleness via the Fever API.

Complements a `systemd_service` monitor on FreshRSS's php-fpm pool rather than
replacing it. That one answers "is PHP-FPM alive"; this one answers "are
feeds actually refreshing", which is a different failure. The case that
motivates it: PHP-FPM stays up and the web UI answers while the refresh cron
silently stops running (a cron misconfiguration, a curl option issue — see
the IPv6 note in freshrss.nix) or one feed's source starts erroring — every
liveness check stays green while the reader quietly goes stale.

The Fever API is used (rather than FreshRSS's own GReader-compatible API)
because it is the one that plainly exposes `last_updated_on_time` per feed,
which is the only field FreshRSS surfaces to say "when did this feed last
actually update" — there is no explicit error flag or message, so staleness
of that timestamp is the proxy signal for "this feed is stuck". The top-level
`last_refreshed_on_time` additionally tells you whether the whole refresh
cycle itself is still running at all — if that is stale, the fault is
systemic rather than one feed's source being broken.

The API requires a separate API password (not the web login password) set
once by hand under Settings > Authentication > API management for the user
this authenticates as — see freshrss.nix for why that step cannot be made
declarative. The Fever auth token is `md5("user:apipassword")`, computed
locally so the API password itself never has to be typed into a request URL.

Config options:
  api_url            Base URL of the FreshRSS instance, as seen from the
                     monitored host (default: http://127.0.0.1:80)
  username           FreshRSS username the API password belongs to
                     (required).
  api_password       The Fever API password (not the login password).
                     Prefer api_password_command.
  api_password_command
                     Command run on the monitored host whose stdout is the
                     API password (e.g. "cat /run/secrets/
                     freshrss_api_password"). Takes precedence over
                     `api_password`.
  feed_stale_warning Hours since a feed's last update at which status is
                     warning (default: 48)
  feed_stale_threshold
                     Hours since a feed's last update at which status is
                     failed (default: 168 — one week)
  refresh_stale_warning
                     Hours since the last full refresh cycle at which
                     status is warning (default: 6)
  api_timeout        Seconds allowed for the remote curl call (default: 10)
"""
import hashlib
import json
import shlex
import time
from typing import Any, Dict, List, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS


def _build_fetch_script(api_url: str, timeout: int, username: str,
                        api_password_command: Optional[str],
                        api_password: Optional[str]) -> str:
    """
    Build a shell script that computes the Fever auth token and fetches the
    feed list. The token is md5("username:apipassword"); computed on the
    remote host so the password never has to appear in Vigil's own process
    or logs, matching how password_command works elsewhere.
    """
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if api_password_command:
        lines.append(f"__pw=$({api_password_command})")
    else:
        lines.append(f"__pw={shlex.quote(api_password or '')}")

    lines.append(f'__token=$(printf "%s:%s" {shlex.quote(username)} "$__pw" | md5sum | cut -d" " -f1)')
    lines.append(
        f'curl -s -m {timeout} -F "api_key=$__token" '
        f'{shlex.quote(base + "/api/fever.php?api&feeds")}'
    )
    return '\n'.join(lines)


def _parse_response(stdout: str) -> Dict[str, Any]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"Fever API response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(data, dict) or 'auth' not in data:
        raise ValueError(f"response missing 'auth': {stdout[:200]!r}")
    if data.get('auth') != 1:
        raise ValueError(
            "Fever API rejected the credentials (check username / api_password_command)")
    return data


def _format_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


_DEFAULT_LAYOUT = [
    ['host_card', 'refresh_card', 'feeds_card'],
    ['stale_card'],
    ['chart'],
    ['events'],
]


class FreshrssPlugin(BasePlugin):
    """Monitors FreshRSS feed-refresh staleness via the Fever API."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:80')
        self.username = config.get('username')
        self.api_password = config.get('api_password')
        self.api_password_command = config.get('api_password_command')
        self.feed_stale_warning = float(config.get('feed_stale_warning', 48))
        self.feed_stale_threshold = float(config.get('feed_stale_threshold', 168))
        self.refresh_stale_warning = float(config.get('refresh_stale_warning', 6))
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        if not self.username:
            self.db_logger.write(
                "No username configured — set username/api_password_command",
                level="ERROR")
            self.set_status('failed')
            return

        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.username,
            self.api_password_command, self.api_password,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Fever API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        feeds: List[Dict[str, Any]] = data.get('feeds', [])
        now = time.time()

        refresh_age_hours = (now - float(data.get('last_refreshed_on_time', 0) or 0)) / 3600.0
        self.db_metrics.metric('refresh_age_hours', refresh_age_hours)
        self.db_metrics.metric('feeds_total', float(len(feeds)))

        stale_warn = []
        stale_fail = []
        for feed in feeds:
            last_updated = feed.get('last_updated_on_time', 0) or 0
            age = (now - float(last_updated)) / 3600.0
            title = feed.get('title', '?')
            if age >= self.feed_stale_threshold:
                stale_fail.append((title, age))
            elif age >= self.feed_stale_warning:
                stale_warn.append((title, age))

        self.db_metrics.metric('feeds_stale', float(len(stale_warn) + len(stale_fail)))

        # --- status ---------------------------------------------------------
        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if refresh_age_hours >= self.refresh_stale_warning:
            problems.append(
                f"refresh cycle stale ({_format_age(refresh_age_hours)} since last run)")
            _escalate('warning')

        if stale_fail:
            names = ', '.join(f"{t} ({_format_age(a)})" for t, a in stale_fail[:3])
            suffix = f" (+{len(stale_fail) - 3} more)" if len(stale_fail) > 3 else ""
            problems.append(f"{len(stale_fail)} feed(s) stale: {names}{suffix}")
            _escalate('failed')
        if stale_warn:
            names = ', '.join(f"{t} ({_format_age(a)})" for t, a in stale_warn[:3])
            suffix = f" (+{len(stale_warn) - 3} more)" if len(stale_warn) > 3 else ""
            problems.append(f"{len(stale_warn)} feed(s) aging: {names}{suffix}")
            _escalate('warning')

        parts = [
            f"{len(feeds)} feed(s)",
            f"refreshed {_format_age(refresh_age_hours)} ago",
        ]
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
        with layout.cell('refresh_card'):
            refresh_label = info_card('LAST REFRESH', '--')
        with layout.cell('feeds_card'):
            feeds_label = info_card('FEEDS', '--')
        with layout.cell('stale_card'):
            stale_label = info_card('STALE FEEDS', '--')
        with layout.cell('chart'):
            history_chart('REFRESH AGE (HOURS)', self.id, 'refresh_age_hours')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            refresh_age = self.latest_metric('refresh_age_hours')
            total       = self.latest_metric('feeds_total')
            stale       = self.latest_metric('feeds_stale')

            if refresh_age:
                refresh_label.text = _format_age(refresh_age.value)
                refresh_label.style(
                    f'color: {STATUS_COLORS["warning" if refresh_age.value >= self.refresh_stale_warning else "online"]}')
            if total:
                feeds_label.text = f'{int(total.value)}'
            if stale:
                count = int(stale.value)
                stale_label.text = str(count)
                stale_label.style(
                    f'color: {STATUS_COLORS["warning" if count else "online"]}')

        safe_timer(5.0, update_cards)
