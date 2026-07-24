import logging
from typing import Optional, Tuple
from vigil.core.common.ssh_connector import SSHConnection

TIMEOUT = 60.0


class SSHController:
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def execute_action(self, command: str,
                             timeout: Optional[float] = None) -> Tuple[int, str, str]:
        deadline = timeout if timeout is not None else self.timeout
        try:
            return await self.ssh.execute(command, timeout=deadline)
        except Exception as e:
            logging.error(f"SSH Controller failed on {self.ssh.host}: {e}")
            return -1, "", str(e)
