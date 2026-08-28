"""Calibre-Web availability, probed over HTTP from the Vigil host by fetching
the /opds feed with basic auth and checking the body actually looks like an
Atom/OPDS feed. Config: url (required, Vigil-reachable), username, password /
password_command, request_timeout. A 200 carrying a real feed is online, with
latency recorded; anything else — a connection error, a 401, or a 200 whose
body is a login or error page instead of a feed — is failed."""

from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret


def _looks_like_opds(body: str) -> bool:
    head = body[:500]
    return '<feed' in head and ('atom' in head.lower() or 'opds' in head.lower())


_DEFAULT_LAYOUT = [
    ['host_card', 'feed_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class CalibreWeb(Plugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'feed_card': {
                'metric': 'feed_ok', 'title': 'OPDS FEED',
                'format': 'calibre_web_ok_text', 'color': 'calibre_web_ok_color',
            },
            'latency_card': {'metric': 'feed_latency_ms', 'title': 'LATENCY', 'format': 'ms0'},
        },
        'chart': {'metric': 'feed_latency_ms', 'title': 'OPDS LATENCY (ms)'},
        'events': True,
    }

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.url = config.get('url')
        self.username = config.get('username', 'vigil')
        # Resolve the secret once, on the Vigil host, so requests() stays pure.
        self.password = resolve_secret(config.get('password'),
                                       config.get('password_command'))
        self.request_timeout = int(config.get('request_timeout', 10))

    def requests(self) -> List[Request]:
        if not self.url:
            return []
        base = self.url.rstrip('/')
        auth = (self.username, self.password or '') if self.username else None
        return [HttpRequest(url=f"{base}/opds", timeout=self.request_timeout, auth=auth)]

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'url' configured")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult(
                metrics={'feed_ok': 0.0},
                logs=[(f"Failed to fetch OPDS feed: {result.error}", "ERROR")],
                status='failed',
            )

        body = result.text
        status = result.status_code or 0
        elapsed_ms = result.elapsed_ms

        metrics = {'feed_status': float(status), 'feed_latency_ms': elapsed_ms}

        if status == 200 and _looks_like_opds(body):
            metrics['feed_ok'] = 1.0
            return CollectResult(
                metrics=metrics,
                logs=[(f"OPDS feed OK ({elapsed_ms:.0f}ms)", "INFO")],
                status='online',
            )

        metrics['feed_ok'] = 0.0
        if status == 401:
            message = ("OPDS request rejected (401) — check the vigil account "
                       "credentials")
        elif status == 200:
            message = ("OPDS request returned 200 but the body was not a valid "
                       "feed — likely a login or error page instead of real data")
        else:
            message = f"OPDS request returned unexpected status {status}"
        return CollectResult(metrics=metrics, logs=[(message, "ERROR")], status='failed')


from vigil.core.ui.spec import register_formatter, register_color_rule


@register_formatter('calibre_web_ok_text')
def _feed_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('calibre_web_ok_color')
def _feed_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
