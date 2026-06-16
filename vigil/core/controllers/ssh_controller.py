import logging
from typing import Tuple, Dict, Any
from vigil.core.ssh import SSHConnection

class SSHController:
    """
    Internal plugin for SSH-based remote control.
    Provides methods to execute remediation or management commands.
    """
    def __init__(self, ssh_conn: SSHConnection):
        self.ssh = ssh_conn

    async def execute_action(self, command: str) -> Tuple[int, str, str]:
        """Executes a control command and returns status."""
        try:
            return self.ssh.execute(command)
        except Exception as e:
            logging.error(f"SSH Controller failed to execute action '{command}': {e}")
            return -1, "", str(e)