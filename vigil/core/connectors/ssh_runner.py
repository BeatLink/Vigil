import logging
from typing import Optional, Tuple
from vigil.core.connectors.ssh_connector import SSHConnection

COLLECT_TIMEOUT = 30.0
CONTROL_TIMEOUT = 60.0

# Backwards-compatible alias: historically ssh_collector.py exported TIMEOUT.
TIMEOUT = COLLECT_TIMEOUT


class _SSHRunner:
    """Shared wrapper around SSHConnection.execute() with error logging."""

    _label = "SSH Runner"

    def __init__(self, ssh_conn: SSHConnection, timeout: float):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def _run(self, command: str, timeout: Optional[float] = None) -> Tuple[int, str, str]:
        deadline = timeout if timeout is not None else self.timeout
        try:
            return await self.ssh.execute(command, timeout=deadline)
        except Exception as e:
            logging.error(f"{self._label} failed on {self.ssh.host}: {e}")
            return -1, "", str(e)


class SSHCollector(_SSHRunner):
    _label = "SSH Collector"

    def __init__(self, ssh_conn: SSHConnection, timeout: float = COLLECT_TIMEOUT):
        super().__init__(ssh_conn, timeout)

    async def fetch_output(self, command: str,
                            timeout: Optional[float] = None) -> Tuple[int, str, str]:
        return await self._run(command, timeout)


class SSHController(_SSHRunner):
    _label = "SSH Controller"

    def __init__(self, ssh_conn: SSHConnection, timeout: float = CONTROL_TIMEOUT):
        super().__init__(ssh_conn, timeout)

    async def execute_action(self, command: str,
                              timeout: Optional[float] = None) -> Tuple[int, str, str]:
        return await self._run(command, timeout)
