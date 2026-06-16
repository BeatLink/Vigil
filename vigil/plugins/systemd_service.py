import logging
from typing import Dict, Any
from vigil.core.plugin import BasePlugin
from vigil.core.ssh import SSHConnection
from vigil.core.database import VigilDatabase

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
        self.ssh = SSHConnection(
            host=target_host,
            username=ssh_config.get('username'),
            key_path=ssh_config.get('key_path'),
            password=ssh_config.get('password'),
            port=ssh_config.get('port', 22)
        )

    async def collect(self) -> str:
        """Fetches recent journalctl logs."""
        command = f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        try:
            with self.ssh as conn:
                status, stdout, stderr = conn.execute(command)
                if status == 0:
                    return stdout
                return f"Error: {stderr}"
        except Exception as e:
            return f"SSH Failure: {e}"

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
            try:
                with self.ssh as conn:
                    status, _, stderr = conn.execute(command)
                    return status == 0
            except Exception:
                return False
        return False