import asyncio
import functools
import logging
from typing import Optional, Tuple
from vigil.core.common.ssh_connector import SSHConnection
from vigil.core.modules.collectors.ssh_collector import TIMEOUT_GRACE

# Default ceiling on a control action (restart a unit, stop a container).
# These are short by nature; anything long-running belongs to JobController,
# which streams output and has no deadline at all.
TIMEOUT = 60.0


class SSHController:
    """
    Internal plugin for SSH-based remote control.
    Provides methods to execute remediation or management commands.
    """
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def execute_action(self, command: str,
                             timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """
        Execute a control command off the event loop and return its status.

        The deadline is passed down to `execute`, which kills the whole ssh
        process group when it fires — `wait_for` alone only stops awaiting, so
        without it a timed-out action would keep running on the target with
        nothing tracking it. The outer wait is a slightly longer backstop for a
        worker thread wedged before its own timeout can fire.
        """
        deadline = timeout if timeout is not None else self.timeout
        try:
            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(self.ssh.execute, command, timeout=deadline),
                ),
                timeout=deadline + TIMEOUT_GRACE,
            )
        except asyncio.TimeoutError:
            logging.error(
                f"SSH action timed out after {deadline}s on {self.ssh.host}: {command!r}"
            )
            return -1, "", f"Timed out after {deadline}s"
        except Exception as e:
            logging.error(f"SSH Controller failed on {self.ssh.host}: {e}")
            return -1, "", str(e)
