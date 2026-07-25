import re
import logging
from typing import List

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, Command, CollectResult, PingRequest, PingResult, Request, Result,
)

_DEFAULT_LAYOUT = [
    ['host_card', 'status_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class Uptime(Plugin):
    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def requests(self) -> List[Request]:
        return [PingRequest(self.target)]

    def parse_results(self, results: List[Result]) -> CollectResult:
        host = self.target
        result: PingResult = results[0]

        if result.exception is not None:
            logging.error(f"Uptime plugin error for {host}: {result.exception}")
            return CollectResult(
                metrics={'up': 0.0},
                logs=[(f"Ping execution failed: {result.exception}", "ERROR")],
                status='failed',
            )

        if result.returncode == 0:
            metrics = {'up': 1.0}
            latency_match = re.search(r'time=([\d.]+)\s*ms', result.stdout)
            if latency_match:
                metrics['latency_ms'] = float(latency_match.group(1))
            return CollectResult(
                metrics=metrics,
                logs=[(f"Host {host} is reachable.", "INFO")],
                status='online',
            )

        err_msg = result.stderr.strip() or "Request timed out"
        return CollectResult(
            metrics={'up': 0.0},
            logs=[(f"Host {host} is unreachable: {err_msg}", "ERROR")],
            status='failed',
        )

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'status_card': {
                'metric': 'up', 'title': 'CURRENT STATUS',
                'on_text': 'ONLINE', 'off_text': 'OFFLINE',
            },
            'latency_card': {
                'metric': 'latency_ms', 'title': 'LAST LATENCY', 'format': 'seconds_ms',
            },
        },
        'chart': {'metric': 'latency_ms', 'title': 'RESPONSE TIME HISTORY (ms)'},
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)
