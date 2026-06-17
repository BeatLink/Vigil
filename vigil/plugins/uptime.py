import asyncio
import platform
import re
import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.theme import COLOR_MAP, TEXT_5XL, FONT_BLACK
from vigil.core.ui.components import info_card, history_chart

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
        """Specialized UI for Uptime monitoring."""
        from nicegui import ui
        from vigil.core.data.database import Metric
        
        with ui.row().classes('w-full gap-4 mb-4'):
            # Target Host Card
            info_card('TARGET HOST', self.target)

            # Status Card
            status_label = info_card('CURRENT STATUS', 'Checking...', value_classes=f'{TEXT_5XL} {FONT_BLACK}')
                
            def update_status():
                last = Metric.select().where(
                    (Metric.collector == self.name) & (Metric.metric_name == 'up')
                ).order_by(Metric.timestamp.desc()).first()
                if last:
                    is_up = last.value > 0.5
                    status_label.text = 'ONLINE' if is_up else 'OFFLINE'
                    status_color = COLOR_MAP['online'] if is_up else COLOR_MAP['failed']
                    status_label.style(f'color: {status_color}')
            ui.timer(2.0, update_status)

            # Latency Card
            latency_label = info_card('LAST LATENCY', '-- ms', value_classes=f'{TEXT_5XL} {FONT_BLACK} text-blue-500')
                
            def update_latency():
                last = Metric.select().where(
                    (Metric.collector == self.name) & (Metric.metric_name == 'latency_ms')
                ).order_by(Metric.timestamp.desc()).first()
                if last:
                    latency_label.text = f"{last.value:.1f} ms"
            ui.timer(2.0, update_latency)
        
        # Latency History Chart
        history_chart('RESPONSE TIME HISTORY (ms)', self.name, 'latency_ms')

        # Call the base implementation to show the logs table below the status cards
        super().render_ui()