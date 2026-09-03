"""FreshRSS feed freshness via the Fever API, queried over HTTP from the
Vigil host using the Fever protocol's md5 username:password token. Config:
api_url (required, Vigil-reachable), username, api_password /
api_password_command, feed_stale_warning, feed_stale_threshold,
refresh_stale_warning (all in hours), api_timeout. The refresh-cycle age is
the newest per-feed fetch time, which is the one signal that proves the
updater is running (FreshRSS fills Fever's last_refreshed_on_time with the
*oldest* feed's fetch, which one paused feed would keep permanently stale).
A stale refresh cycle or aging feeds are warning; any feed older than
feed_stale_threshold is failed, as are transport, HTTP, and auth errors."""

import hashlib
import json
import time
from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import StatusAccumulator, resolve_secret


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


def _survey_feeds(feeds: List[Dict[str, Any]], now: float,
                  feed_stale_warning: float, feed_stale_threshold: float):
    """Bucket feeds into (title, age-in-hours) warn/fail lists by how long since each last updated."""
    stale_warn = []
    stale_fail = []
    for feed in feeds:
        last_updated = feed.get('last_updated_on_time', 0) or 0
        age = (now - float(last_updated)) / 3600.0
        title = feed.get('title', '?')
        if age >= feed_stale_threshold:
            stale_fail.append((title, age))
        elif age >= feed_stale_warning:
            stale_warn.append((title, age))
    return stale_warn, stale_fail


def _newest_fetch(feeds: List[Dict[str, Any]], fallback: Any) -> float:
    """The most recent per-feed fetch time, falling back to the Fever field when there are no feeds."""
    times = [float(feed.get('last_updated_on_time', 0) or 0) for feed in feeds]
    return max(times) if times else float(fallback or 0)


def _accumulate_freshness(refresh_age_hours: float, stale_warn, stale_fail,
                          refresh_stale_warning: float) -> StatusAccumulator:
    """Fold the refresh-cycle age and stale-feed buckets into problem lines and a worst-of status."""
    acc = StatusAccumulator()
    if refresh_age_hours >= refresh_stale_warning:
        acc.escalate('warning',
                     f"refresh cycle stale ({_format_age(refresh_age_hours)} since last run)")
    if stale_fail:
        names = ', '.join(f"{title} ({_format_age(age)})" for title, age in stale_fail[:3])
        suffix = f" (+{len(stale_fail) - 3} more)" if len(stale_fail) > 3 else ""
        acc.escalate('failed', f"{len(stale_fail)} feed(s) stale: {names}{suffix}")
    if stale_warn:
        names = ', '.join(f"{title} ({_format_age(age)})" for title, age in stale_warn[:3])
        suffix = f" (+{len(stale_warn) - 3} more)" if len(stale_warn) > 3 else ""
        acc.escalate('warning', f"{len(stale_warn)} feed(s) aging: {names}{suffix}")
    return acc


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
        """Turns the single Fever API HTTP result into a CollectResult with
        refresh-age and feed-count metrics, one summary log line, and a status
        where any feed past feed_stale_threshold is failed and a stale refresh
        cycle or aging feeds are warning."""
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
        refresh_age_hours = (now - _newest_fetch(feeds, data.get('last_refreshed_on_time'))) / 3600.0

        stale_warn, stale_fail = _survey_feeds(
            feeds, now, self.feed_stale_warning, self.feed_stale_threshold)

        metrics = {
            'refresh_age_hours': refresh_age_hours,
            'feeds_total': float(len(feeds)),
            'feeds_stale': float(len(stale_warn) + len(stale_fail)),
        }

        acc = _accumulate_freshness(refresh_age_hours, stale_warn, stale_fail,
                                    self.refresh_stale_warning)

        parts = [
            f"{len(feeds)} feed(s)",
            f"refreshed {_format_age(refresh_age_hours)} ago",
        ]
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), acc.log_level)], status=acc.status)

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


from vigil.core.ui.spec import register_formatter


@register_formatter('freshrss_age')
def _refresh_age_text(v):
    return '--' if v is None else _format_age(v)
