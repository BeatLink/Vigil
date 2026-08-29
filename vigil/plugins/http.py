"""Generic endpoint probe: is this URL reachable and answering sanely, and how
fast. An http(s) url is fetched from the Vigil host (method, headers, body and
basic auth configurable); a ws(s) url is probed with websocat on the target,
sampled locally by the agent on agent-backed hosts. Config: url (required),
method, headers, body (request body, or the text sent into the websocket),
username, password / password_command, request_timeout, check_title, and
expect — status (int or list, http only, default 200), body_contains (all
must appear, case-insensitive) and body_contains_any (at least one). A reply
matching every expectation is online, with latency recorded on the http path;
anything else is failed — there is no warning tier."""

import shlex
from typing import Any, Dict, List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, CollectResult, Command, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret


def _as_list(value) -> list:
    """Normalize a scalar-or-list config value into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _body_mismatch(body: str, expect: Dict[str, Any]) -> bool:
    """Whether the body fails the expect block's case-insensitive contains checks."""
    lowered = body.lower()
    needed = [str(s).lower() for s in _as_list(expect.get('body_contains'))]
    if any(s not in lowered for s in needed):
        return True
    any_of = [str(s).lower() for s in _as_list(expect.get('body_contains_any'))]
    return bool(any_of) and not any(s in lowered for s in any_of)


class HttpCheck(Plugin):
    SAMPLED = True

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # An http(s) url is fetched from the Vigil host and must be reachable from it.
        self.url = config.get('url')
        self.is_ws = bool(self.url) and self.url.split(':', 1)[0] in ('ws', 'wss')
        self.method = config.get('method', 'GET')
        self.headers = config.get('headers') or {}
        self.body = config.get('body')
        self.username = config.get('username')
        # Resolve the secret once, on the Vigil host, so requests() stays pure.
        self.password = resolve_secret(config.get('password'),
                                       config.get('password_command'))
        self.request_timeout = int(config.get('request_timeout', 10))
        self.expect = config.get('expect') or {}
        self.check_title = config.get('check_title', 'PROBE')

    def commands(self) -> List[Command]:
        if not (self.is_ws and self.url):
            return []
        send = f'echo {shlex.quote(self.body)} | ' if self.body else ''
        return [Command(
            f'{send}timeout {self.request_timeout} websocat -n1 {shlex.quote(self.url)}'
        )]

    def requests(self) -> List[Request]:
        if self.is_ws:
            return self.commands()
        if not self.url:
            return []
        auth = (self.username, self.password or '') if self.username else None
        return [HttpRequest(
            url=self.url, method=self.method, timeout=self.request_timeout,
            headers=self.headers, body=self.body, auth=auth,
        )]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        result = results[0]
        if result.exit_code != 0:
            return CollectResult(
                metrics={'probe_ok': 0.0},
                logs=[(
                    f"WebSocket probe failed: {result.stderr.strip() or 'timed out or connection refused'}",
                    "ERROR",
                )],
                status='failed',
            )
        if _body_mismatch(result.stdout, self.expect):
            return CollectResult(
                metrics={'probe_ok': 0.0},
                logs=[(f"WebSocket reply did not match 'expect': {result.stdout[:200]!r}", "ERROR")],
                status='failed',
            )
        return CollectResult(
            metrics={'probe_ok': 1.0},
            logs=[("WebSocket probe OK", "INFO")],
            status='online',
        )

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'url' configured")
        if self.is_ws:
            return self.parse(results)

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult(
                metrics={'probe_ok': 0.0},
                logs=[(f"Probe request failed: {result.error}", "ERROR")],
                status='failed',
            )

        status = result.status_code or 0
        elapsed_ms = result.elapsed_ms
        metrics = {'probe_status': float(status), 'probe_latency_ms': elapsed_ms}

        expected = [int(s) for s in _as_list(self.expect.get('status'))] or [200]
        if status not in expected:
            metrics['probe_ok'] = 0.0
            hint = ' — check the configured credentials' if status == 401 else ''
            return CollectResult(
                metrics=metrics,
                logs=[(f"Probe returned unexpected status {status}{hint}", "ERROR")],
                status='failed',
            )

        if _body_mismatch(result.text, self.expect):
            metrics['probe_ok'] = 0.0
            return CollectResult(
                metrics=metrics,
                logs=[(
                    f"Probe returned {status} but the body did not match 'expect' — "
                    "likely a login or error page instead of real data",
                    "ERROR",
                )],
                status='failed',
            )

        metrics['probe_ok'] = 1.0
        return CollectResult(
            metrics=metrics,
            logs=[(f"Probe OK ({status}, {elapsed_ms:.0f}ms)", "INFO")],
            status='online',
        )

    @property
    def UI_SPEC(self):
        probe_card = {
            'metric': 'probe_ok', 'title': self.check_title,
            'format': 'probe_ok_text', 'color': 'probe_ok_color',
        }
        if self.is_ws:
            return {
                'layout': [['host_card', 'probe_card'], ['chart'], ['events']],
                'cards': {'probe_card': probe_card},
                'chart': {'metric': 'probe_ok', 'title': f'{self.check_title} OK'},
                'events': True,
            }
        return {
            'layout': [['host_card', 'probe_card', 'latency_card'], ['chart'], ['events']],
            'cards': {
                'probe_card': probe_card,
                'latency_card': {'metric': 'probe_latency_ms', 'title': 'LATENCY', 'format': 'ms0'},
            },
            'chart': {'metric': 'probe_latency_ms', 'title': f'{self.check_title} LATENCY (ms)'},
            'events': True,
        }


from vigil.core.ui.spec import register_formatter, register_color_rule


@register_formatter('probe_ok_text')
def _probe_text(v):
    """Render a probe_ok metric as OK/FAILED."""
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('probe_ok_color')
def _probe_color(v):
    """Color a probe_ok metric online/failed."""
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
