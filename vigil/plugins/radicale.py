"""Radicale CalDAV/CardDAV health, probed with one authenticated PROPFIND
request over HTTP from the Vigil host. Config: url (required,
Vigil-reachable), username, password / password_command, request_timeout. A
207 Multi-Status reply is online, with latency recorded; anything else — a
connection failure, a 401 from a bad vigil htpasswd entry, or an unexpected
status — is failed. There is no warning tier."""

from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret

_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>'
)


_DEFAULT_LAYOUT = [
    ['host_card', 'propfind_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class Radicale(Plugin):
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
        return [HttpRequest(
            url=f"{base}/", method="PROPFIND", timeout=self.request_timeout,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            body=_PROPFIND_BODY, auth=auth,
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'url' configured")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult(
                metrics={'propfind_ok': 0.0},
                logs=[(f"Failed to run PROPFIND probe: {result.error}", "ERROR")],
                status='failed',
            )

        body = result.text
        status = result.status_code or 0
        elapsed_ms = result.elapsed_ms

        metrics = {'propfind_status': float(status), 'propfind_latency_ms': elapsed_ms}

        if status == 207:
            metrics['propfind_ok'] = 1.0
            return CollectResult(
                metrics=metrics,
                logs=[(f"PROPFIND OK (207 Multi-Status, {elapsed_ms:.0f}ms)", "INFO")],
                status='online',
            )

        metrics['propfind_ok'] = 0.0
        if status == 401:
            msg = ("PROPFIND rejected (401) — check the vigil htpasswd entry "
                   "is present and matches password_command")
        elif status == 0:
            msg = "PROPFIND got no response (connection failed)"
        else:
            msg = f"PROPFIND returned unexpected status {status}: {body[:200]}"
        return CollectResult(metrics=metrics, logs=[(msg, "ERROR")], status='failed')

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'propfind_card': {
                'metric': 'propfind_ok', 'title': 'PROPFIND',
                'format': 'radicale_ok_text', 'color': 'radicale_ok_color',
            },
            'latency_card': {'metric': 'propfind_latency_ms', 'title': 'LATENCY', 'format': 'ms0'},
        },
        'chart': {'metric': 'propfind_latency_ms', 'title': 'PROPFIND LATENCY (ms)'},
        'events': True,
    }


from vigil.core.ui.spec import register_formatter, register_color_rule


@register_formatter('radicale_ok_text')
def _propfind_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('radicale_ok_color')
def _propfind_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
