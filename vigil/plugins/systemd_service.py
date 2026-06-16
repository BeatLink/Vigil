import logging
from typing import Dict, Any
from vigil.core.plugin import BasePlugin
from vigil.core.ssh import SSHConnection
from vigil.core.database import VigilDatabase
from vigil.core.collectors.ssh_collector import SSHCollector
from vigil.core.controllers.ssh_controller import SSHController

class SystemdPlugin(BasePlugin):
    """
    A unified plugin for Systemd. 
    Handles log collection, status monitoring, and service control.
    """
    def __init__(self, name: str, target_host: str, interval: int,
                 service_name: str, lines: int, ssh_config: Dict[str, Any],
                 db_manager: VigilDatabase):
        super().__init__(name, target_host, interval)
        self.service_name = service_name
        self.lines = lines
        self.db_manager = db_manager
        
        # Internal SSH connection utility
        self.ssh_conn = SSHConnection(
            host=target_host,
            username=ssh_config.get('username'),
            key_path=ssh_config.get('key_path'),
            password=ssh_config.get('password'),
            port=ssh_config.get('port', 22)
        )
        self.collector = SSHCollector(self.ssh_conn)
        self.controller = SSHController(self.ssh_conn)

    async def collect(self) -> str:
        """Fetches recent journalctl logs."""
        command = f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        status, stdout, stderr = await self.collector.fetch_output(command)
        if status == 0:
            return stdout
        return f"Error: {stderr}"

    async def alert(self, data: str):
        """Parses logs for keywords and records events."""
        lines = data.splitlines()
        for line in lines:
            level = "INFO"
            if any(k in line.upper() for k in ["ERROR", "FAIL", "CRITICAL"]):
                level = "ERROR"
            
            self.db_manager.insert_event(
                level=level,
                message=line,
                target=self.target
            )

    async def control(self, action: str, **kwargs) -> bool:
        """Remediates service issues (e.g., restart)."""
        if action == "restart":
            command = f"systemctl restart {self.service_name}"
            status, _, _ = await self.controller.execute_action(command)
            return status == 0
        return False