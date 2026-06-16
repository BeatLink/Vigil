import asyncio
import platform
import re
import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin

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
                
                # Attempt to extract latency from output (e.g., "time=12.3 ms")
                latency_match = re.search(r'time=([\d.]+)\s*ms', output)
                if latency_match:
                    latency = float(latency_match.group(1))
                    self.db_metrics.metric("latency_ms", latency)
            else:
                err_msg = stderr.decode().strip() or "Request timed out"
                self.db_logger.write(f"Host {host} is unreachable: {err_msg}", level="ERROR")
                self.db_metrics.metric("up", 0.0)

        except Exception as e:
            logging.error(f"Uptime plugin error for {host}: {e}")
            self.db_logger.write(f"Ping execution failed: {str(e)}", level="ERROR")
            self.db_metrics.metric("up", 0.0)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Basic uptime monitoring does not currently support remediation actions."""
        return False