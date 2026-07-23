import logging
from typing import Optional, Tuple
from vigil.core.common.ssh_connector import SSHConnection

# Default ceiling on a control action (restart a unit, stop a container).
# These are short by nature; anything long-running belongs to JobController,
# which streams output and has no deadline at all.
TIMEOUT = 60.0


class SSHController:
    """
    Internal plugin for SSH-based remote control.
    Provides methods to execute remediation or management commands.

    Thin pass-through to SSHConnection.execute, same as SSHCollector — see
    that class's docstring for why the previous thread-pool wrapping is gone
    now that SSHConnection is natively async.
    """
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def execute_action(self, command: str,
                             timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """
        Execute a control command and return its status.

        The deadline is passed down to `execute`, which kills the remote
        process when it fires (verified empirically — see
        ssh_connector.py's module docstring), so a timed-out action leaves
        nothing running on the target.
        """
        deadline = timeout if timeout is not None else self.timeout
        try:
            return await self.ssh.execute(command, timeout=deadline)
        except Exception as e:
            logging.error(f"SSH Controller failed on {self.ssh.host}: {e}")
            return -1, "", str(e)
