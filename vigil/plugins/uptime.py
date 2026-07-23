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
    """
    A simple uptime plugin that checks host availability via ICMP ping.
    Reports availability status and latency (ms) to the database.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)


    async def on_collect(self):
        """Executes a ping command and records the result."""
        host = self.target

        # Determine the correct ping flag based on the operating system
        is_windows = platform.system().lower() == 'windows'
        # -c/-n 1: single packet, -W 2: 2 second timeout
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

                # Attempt to extract latency from output (e.g., "time=12.3 ms")
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
        """Basic uptime monitoring does not currently support remediation actions."""
        return False


class UptimeUIPlugin(UIPlugin):
    """Dashboard rendering for the uptime monitor."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['latency_ms'])

        def _latency_or_dash(v):
            return '-- ms' if v is None else f'{v:.1f} ms'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                page,
                metric_name='up',
                title='CURRENT STATUS',
                on_text='ONLINE',
                off_text='OFFLINE'
            )
        with layout.cell('latency_card'):
            info_card('LAST LATENCY', '-- ms').bind_text_from(
                page.model, ('metrics', 'latency_ms'), backward=_latency_or_dash)
        with layout.cell('chart'):
            history_chart(page, 'RESPONSE TIME HISTORY (ms)', self.id, 'latency_ms')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        page.start()
