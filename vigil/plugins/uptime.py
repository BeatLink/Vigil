import asyncio
import platform
import re
import logging
from typing import Any, Callable, Dict, List, Optional

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_DEFAULT_LAYOUT = [
    ['host_card', 'status_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class UptimeCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def local_call(self) -> Optional[Callable[[], Any]]:
        host = self.target

        async def _ping():
            is_windows = platform.system().lower() == 'windows'
            cmd = ['ping', '-n' if is_windows else '-c', '1', '-W', '2', host]
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                return {
                    'exception': None,
                    'returncode': process.returncode,
                    'stdout': stdout.decode(),
                    'stderr': stderr.decode(),
                }
            except Exception as e:
                return {'exception': str(e), 'returncode': None, 'stdout': '', 'stderr': ''}

        return _ping

    def parse_local(self, result: Any) -> CollectResult:
        host = self.target

        if result['exception'] is not None:
            logging.error(f"Uptime plugin error for {host}: {result['exception']}")
            return CollectResult(
                metrics={'up': 0.0},
                logs=[(f"Ping execution failed: {result['exception']}", "ERROR")],
                status='failed',
            )

        if result['returncode'] == 0:
            metrics = {'up': 1.0}
            latency_match = re.search(r'time=([\d.]+)\s*ms', result['stdout'])
            if latency_match:
                metrics['latency_ms'] = float(latency_match.group(1))
            return CollectResult(
                metrics=metrics,
                logs=[(f"Host {host} is reachable.", "INFO")],
                status='online',
            )

        err_msg = result['stderr'].strip() or "Request timed out"
        return CollectResult(
            metrics={'up': 0.0},
            logs=[(f"Host {host} is unreachable: {err_msg}", "ERROR")],
            status='failed',
        )


class UptimeUIPlugin(UIPlugin):
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
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)
