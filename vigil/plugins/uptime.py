import asyncio
import platform
import re
import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.main_dashboard import COLOR_MAP

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
                self.set_status('success')
                
                # Attempt to extract latency from output (e.g., "time=12.3 ms")
                latency_match = re.search(r'time=([\d.]+)\s*ms', output)
                if latency_match:
                    latency = float(latency_match.group(1))
                    self.db_metrics.metric("latency_ms", latency)
            else:
                err_msg = stderr.decode().strip() or "Request timed out"
                self.db_logger.write(f"Host {host} is unreachable: {err_msg}", level="ERROR")
                self.db_metrics.metric("up", 0.0)
                self.set_status('fail')

        except Exception as e:
            logging.error(f"Uptime plugin error for {host}: {e}")
            self.db_logger.write(f"Ping execution failed: {str(e)}", level="ERROR")
            self.db_metrics.metric("up", 0.0)
            self.set_status('fail')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Basic uptime monitoring does not currently support remediation actions."""
        return False

    def render_ui(self):
        """Specialized UI for Uptime monitoring."""
        from nicegui import ui
        from vigil.core.data.database import Metric
        
        with ui.row().classes('w-full gap-4 mb-4'):
            # Target Host Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-md'):
                ui.label('TARGET HOST').classes('text-xs text-gray-400 font-bold')
                ui.label(self.target).classes('text-3xl font-black text-slate-500')

            # Status Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-md'):
                ui.label('CURRENT STATUS').classes('text-xs text-gray-400 font-bold')
                status_label = ui.label('Checking...').classes('text-5xl font-black')
                
                def update_status():
                    last = Metric.select().where(
                        (Metric.collector == self.name) & (Metric.metric_name == 'up')
                    ).order_by(Metric.timestamp.desc()).first()
                    if last:
                        is_up = last.value > 0.5
                        status_label.text = 'ONLINE' if is_up else 'OFFLINE'
                        status_color = COLOR_MAP['success'] if is_up else COLOR_MAP['fail']
                        status_label.style(f'color: {status_color}')
                ui.timer(2.0, update_status)

            # Latency Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-md'):
                ui.label('LAST LATENCY').classes('text-xs text-gray-400 font-bold')
                latency_label = ui.label('-- ms').classes('text-5xl font-black text-blue-500')
                
                def update_latency():
                    last = Metric.select().where(
                        (Metric.collector == self.name) & (Metric.metric_name == 'latency_ms')
                    ).order_by(Metric.timestamp.desc()).first()
                    if last:
                        latency_label.text = f"{last.value:.1f} ms"
                ui.timer(2.0, update_latency)
        
        # Call the base implementation to show the logs table below the status cards
        super().render_ui()