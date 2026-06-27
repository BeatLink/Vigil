import asyncio
import logging
from typing import Tuple
from vigil.core.common.ssh_connector import SSHConnection

TIMEOUT = 30.0

class SSHCollector:
    """
    Internal plugin for SSH-based data collection.
    Provides high-level methods to fetch raw data from remote targets.
    """
    def __init__(self, ssh_conn: SSHConnection):
        self.ssh = ssh_conn

    async def fetch_output(self, command: str) -> Tuple[int, str, str]:
        """Executes a collection command off the event loop and returns the results."""
        try:
            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, self.ssh.execute, command),
                timeout=TIMEOUT
            )
        except asyncio.TimeoutError:
            logging.error(f"SSH command timed out after {TIMEOUT}s on {self.ssh.host}: {command!r}")
            return -1, "", f"Timed out after {TIMEOUT}s"
        except Exception as e:
            logging.error(f"SSH Collector failed on {self.ssh.host}: {e}")
            return -1, "", str(e)