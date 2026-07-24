import hashlib
import json
import shlex
import time
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin


def _build_fetch_script(api_url: str, timeout: int, username: str,
                        api_password_command: Optional[str],
                        api_password: Optional[str]) -> str:
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


class FreshrssCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:80')
        self.username = config.get('username')
        self.api_password = config.get('api_password')
        self.api_password_command = config.get('api_password_command')
        self.feed_stale_warning = float(config.get('feed_stale_warning', 48))
        self.feed_stale_threshold = float(config.get('feed_stale_threshold', 168))
        self.refresh_stale_warning = float(config.get('refresh_stale_warning', 6))
        self.api_timeout = int(config.get('api_timeout', 10))

    def commands(self) -> List[Command]:
        if not self.username:
            return []
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.username,
            self.api_password_command, self.api_password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not self.username:
            return CollectResult.failed(
                "No username configured — set username/api_password_command")

        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Fever API: {stderr.strip()}")

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        feeds: List[Dict[str, Any]] = data.get('feeds', [])
        now = time.time()

        refresh_age_hours = (now - float(data.get('last_refreshed_on_time', 0) or 0)) / 3600.0
        metrics = {'refresh_age_hours': refresh_age_hours, 'feeds_total': float(len(feeds))}

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

        metrics['feeds_stale'] = float(len(stale_warn) + len(stale_fail))

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
        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), log_level)], status=level)


class FreshrssUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.refresh_stale_warning = float(config.get('refresh_stale_warning', 6))

        from vigil.web.ui.spec import register_color_rule
        self._color_rule_name = f'freshrss_refresh_stale_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _refresh_color(v, _warning=self.refresh_stale_warning):
            if v is None:
                return None
            return 'warning' if v >= _warning else 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'refresh_card': {
                    'metric': 'refresh_age_hours', 'title': 'LAST REFRESH',
                    'format': 'freshrss_age', 'color': self._color_rule_name,
                },
                'feeds_card': {'metric': 'feeds_total', 'title': 'FEEDS', 'format': 'int'},
                'stale_card': {
                    'metric': 'feeds_stale', 'title': 'STALE FEEDS',
                    'format': 'int', 'color': 'nonzero_warning',
                },
            },
            'chart': {'metric': 'refresh_age_hours', 'title': 'REFRESH AGE (HOURS)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter


@register_formatter('freshrss_age')
def _refresh_age_text(v):
    return '--' if v is None else _format_age(v)
