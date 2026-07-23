import logging
from typing import Optional, Tuple
from vigil.core.common.ssh_connector import SSHConnection

# Default wall-clock ceiling on a single collection command.
#
# Most collectors read a file or run a quick shell pipeline, where anything
# past a few seconds means the host is in trouble; 30s covers those with room
# to spare. Monitors whose commands are legitimately slow (borg against a
# repository busy with its own maintenance) raise it per instance via the
# `timeout` config key rather than inflating this for everyone — a long global
# default would leave a genuinely dead host marked healthy for minutes.
#
# The deadline is only half the protection: SSHConnection.execute kills the
# remote process (verified empirically — see ssh_connector.py's module
# docstring) when it fires, so a timed-out command leaves nothing running on
# either end.
TIMEOUT = 30.0


class SSHCollector:
    """
    Internal plugin for SSH-based data collection.
    Provides high-level methods to fetch raw data from remote targets.

    Previously wrapped SSHConnection.execute in run_in_executor plus a
    dedicated thread pool and a global 16-connection semaphore, because
    execute() shelled out to a blocking `ssh` subprocess. Now that
    SSHConnection is natively async (asyncssh — see ssh_connector.py) and
    bounds its own per-host channel concurrency internally, none of that
    plumbing is needed here: this is a thin pass-through.
    """
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def fetch_output(self, command: str,
                           timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """
        Execute a collection command and return its results.

        `timeout` overrides the collector's default for one call — used where a
        single monitor knows some of its commands are slower than others.
        """
        deadline = timeout if timeout is not None else self.timeout
        try:
            return await self.ssh.execute(command, timeout=deadline)
        except Exception as e:
            logging.error(f"SSH Collector failed on {self.ssh.host}: {e}")
            return -1, "", str(e)
