import asyncio
import logging
from typing import Tuple
from vigil.core.common.ssh_connector import SSHConnection

TIMEOUT = 30.0

class SSHController:
    """
    Internal plugin for SSH-based remote control.
    Provides methods to execute remediation or management commands.
    """
    def __init__(self, ssh_conn: SSHConnection):
        self.ssh = ssh_conn

    async def execute_action(self, command: str) -> Tuple[int, str, str]:
        """Executes a control command off the event loop and returns status."""
        try:
            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, self.ssh.execute, command),
                timeout=TIMEOUT
            )
        except asyncio.TimeoutError:
            logging.error(f"SSH action timed out after {TIMEOUT}s on {self.ssh.host}: {command!r}")
            return -1, "", f"Timed out after {TIMEOUT}s"
        except Exception as e:
            logging.error(f"SSH Controller failed on {self.ssh.host}: {e}")
            return -1, "", str(e)