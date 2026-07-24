import logging
from typing import Optional, Tuple
from vigil.core.common.ssh_connector import SSHConnection

TIMEOUT = 30.0


class SSHCollector:
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def fetch_output(self, command: str,
                           timeout: Optional[float] = None) -> Tuple[int, str, str]:
        deadline = timeout if timeout is not None else self.timeout
        try:
            return await self.ssh.execute(command, timeout=deadline)
        except Exception as e:
            logging.error(f"SSH Collector failed on {self.ssh.host}: {e}")
            return -1, "", str(e)
