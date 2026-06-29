import asyncio
import platform
import re
import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart

_DEFAULT_LAYOUT = {
    'grid_columns': 3,
    'widgets': {
        'host_card':    {'col_span': 1},
        'status_card':  {'col_span': 1},
        'latency_card': {'col_span': 1},
        'chart':        {'col_span': 3},
        'logs':         {'col_span': 3},
    }
}


class UptimePlugin(BasePlugin):
    """
    A simple uptime plugin that checks host availability via ICMP ping.
    Reports availability status and latency (ms) to the database.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        
        # Access internal database loggers initialized by BasePlugin
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

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

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.layout import PluginLayout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT)

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                metric_name='up',
                title='CURRENT STATUS',
                on_text='ONLINE',
                off_text='OFFLINE'
            )
        with layout.cell('latency_card'):
            latency_label = info_card('LAST LATENCY', '-- ms')
        with layout.cell('chart'):
            history_chart('RESPONSE TIME HISTORY (ms)', self.name, 'latency_ms')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_latency():
            last = Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == 'latency_ms')
            ).order_by(Metric.timestamp.desc()).first()
            if last:
                latency_label.text = f"{last.value:.1f} ms"

        ui.timer(2.0, update_latency)