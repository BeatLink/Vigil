import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin

class SystemdPlugin(BasePlugin):
    """
    A unified plugin for Systemd. 
    Handles log collection, status monitoring, and service control.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        
        # Extract settings from config
        self.service_name = config.get('service_name')
        self.lines = config.get('lines', 10)
        
        # Access internal modules
        self.ssh_collector = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

    async def on_collect(self):
        """Fetches recent journalctl logs."""
        command = f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        status, stdout, stderr = await self.ssh_collector.fetch_output(command)
        if status == 0:
            for line in stdout.splitlines():
                level = "INFO"
                if any(k in line.upper() for k in ["ERROR", "FAIL", "CRITICAL"]):
                    level = "ERROR"
                self.db_logger.write(line, level=level)
        else:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")

    def get_actions(self) -> List[Dict[str, str]]:
        """Exposes available actions to the engine/UI."""
        return [
            {
                "name": "Restart Service",
                "action_id": "restart_service"
            },
            {
                "name": "Stop Service",
                "action_id": "stop_service"
            }
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Remediates service issues (e.g., restart)."""
        if action_id == "restart_service":
            command = f"systemctl restart {self.service_name}"
            status, _, stderr = await self.ssh_controller.execute_action(command)
            if status != 0:
                self.db_logger.write(f"Restart failed: {stderr}", level="ERROR")
            return status == 0
            
        return False