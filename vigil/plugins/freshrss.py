import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, Command, CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret


def _fever_api_key(username: str, password: str) -> str:
    """The Fever API authenticates with an md5 hash of 'username:password',
    sent as a form field. (This is the Fever protocol's own scheme, not a
    security choice by Vigil.)"""
    return hashlib.md5(f"{username}:{password}".encode()).hexdigest()


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


class Freshrss(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.api_url = config.get('api_url')
        self.username = config.get('username')
        # Resolve the API password once, on the Vigil host, so requests() stays pure.
        self.api_password = resolve_secret(config.get('api_password'),
                                           config.get('api_password_command'))
        self.feed_stale_warning = float(config.get('feed_stale_warning', 48))
        self.feed_stale_threshold = float(config.get('feed_stale_threshold', 168))
        self.refresh_stale_warning = float(config.get('refresh_stale_warning', 6))
        self.api_timeout = int(config.get('api_timeout', 10))

        from vigil.core.ui.spec import register_color_rule
        self._color_rule_name = f'freshrss_refresh_stale_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _refresh_color(v, _warning=self.refresh_stale_warning):
            if v is None:
                return None
            return 'warning' if v >= _warning else 'online'

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def requests(self) -> List[Request]:
        if not self.api_url or not self.username:
            return []
        base = self.api_url.rstrip('/')
        token = _fever_api_key(self.username, self.api_password or '')
        return [HttpRequest(
            url=f"{base}/api/fever.php?api&feeds", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=f"api_key={token}", timeout=self.api_timeout,
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not self.api_url:
            return CollectResult.failed("No 'api_url' configured")
        if not self.username:
            return CollectResult.failed(
                "No username configured — set username/api_password_command")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult.failed(f"Failed to query Fever API: {result.error}")
        if result.status_code != 200:
            return CollectResult.failed(
                f"Fever API returned HTTP {result.status_code}")

        try:
            data = _parse_response(result.text)
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
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.spec import register_formatter


@register_formatter('freshrss_age')
def _refresh_age_text(v):
    return '--' if v is None else _format_age(v)
