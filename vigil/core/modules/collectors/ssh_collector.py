import logging
from typing import Tuple, Dict, Any
from vigil.core.common.ssh_connector import SSHConnection

class SSHCollector:
    """
    Internal plugin for SSH-based data collection.
    Provides high-level methods to fetch raw data from remote targets.
    """
    def __init__(self, ssh_conn: SSHConnection):
        self.ssh = ssh_conn

    async def fetch_output(self, command: str) -> Tuple[int, str, str]:
        """Executes a collection command and returns the results."""
        try:
            return self.ssh.execute(command)
        except Exception as e:
            logging.error(f"SSH Collector failed to execute command '{command}': {e}")
            return -1, "", str(e)