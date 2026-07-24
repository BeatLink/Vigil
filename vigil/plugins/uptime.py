import asyncio
import platform
import re
import logging
from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_DEFAULT_LAYOUT = [
    ['host_card', 'status_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class UptimeCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)


    async def on_collect(self):
        host = self.target

        is_windows = platform.system().lower() == 'windows'
        cmd = ['ping', '-n' if is_windows else '-c', '1', '-W', '2', host]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                output = stdout.decode()
                self.db_logger.write(f"Host {host} is reachable.", level="INFO")
                self.db_metrics.metric("up", 1.0)
                self.set_status('online')

                latency_match = re.search(r'time=([\d.]+)\s*ms', output)
                if latency_match:
                    latency = float(latency_match.group(1))
                    self.db_metrics.metric("latency_ms", latency)
            else:
                err_msg = stderr.decode().strip() or "Request timed out"
                self.db_logger.write(f"Host {host} is unreachable: {err_msg}", level="ERROR")
                self.db_metrics.metric("up", 0.0)
                self.set_status('failed')

        except Exception as e:
            logging.error(f"Uptime plugin error for {host}: {e}")
            self.db_logger.write(f"Ping execution failed: {str(e)}", level="ERROR")
            self.db_metrics.metric("up", 0.0)
            self.set_status('failed')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


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
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
