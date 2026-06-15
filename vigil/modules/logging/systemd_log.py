import logging
import re
from typing import Dict, Any, List

from vigil.core.collector import BaseCollector
from vigil.core.ssh import SSHConnection
from vigil.core.database import VigilDatabase

class SystemdLogCollector(BaseCollector):
    """
    A collector for fetching systemd service logs from a remote host via SSH.
    Stores logs as Event records in the database.
    """
    def __init__(self, name: str, target_host: str, interval: int,
                 service_name: str, lines: int, ssh_config: Dict[str, Any],
                 db_manager: VigilDatabase):
        super().__init__(name, target_host, interval)
        self.service_name = service_name
        self.lines = lines
        self.ssh_config = ssh_config
        self.db_manager = db_manager
        self.ssh_connection = SSHConnection(
            host=target_host,
            username=ssh_config.get('username'),
            key_path=ssh_config.get('key_path'),
            password=ssh_config.get('password'),
            port=ssh_config.get('port', 22)
        )
        logging.debug(f"SystemdLogCollector '{self.name}' initialized for service '{self.service_name}' on {self.target}")

    async def collect(self) -> List[Dict[str, str]]:
        """
        Connects to the remote host, fetches systemd logs for the configured service,
        and stores them as events.
        """
        collected_logs = []
        command = f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        
        try:
            with self.ssh_connection as ssh:
                exit_status, stdout, stderr = ssh.execute(command)

                if exit_status == 0:
                    for line in stdout.splitlines():
                        # Basic parsing: journalctl output often starts with timestamp
                        # Example: "Jan 01 12:34:56 hostname service_name[PID]: message"
                        # We'll just store the full line as the message for now.
                        self.db_manager.insert_event(
                            level="INFO", # journalctl doesn't always provide explicit levels easily
                            message=line,
                            target=self.target
                        )
                        collected_logs.append({"target": self.target, "service": self.service_name, "log": line})
                    logging.info(f"Collected {len(stdout.splitlines())} log lines for service '{self.service_name}' on {self.target}")
                else:
                    logging.error(f"Failed to collect logs for '{self.service_name}' on {self.target}: {stderr}")
                    self.db_manager.insert_event("ERROR", f"Failed to collect logs for {self.service_name}: {stderr}", self.target)
        except Exception as e:
            logging.error(f"SSH or command execution error for '{self.service_name}' on {self.target}: {e}")
            self.db_manager.insert_event("CRITICAL", f"SSH error collecting logs for {self.service_name}: {e}", self.target)
        return collected_logs