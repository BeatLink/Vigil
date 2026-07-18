import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
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
# whole process group when it fires, so a timed-out command leaves nothing
# running on either end.
TIMEOUT = 30.0

# Extra time the async wait allows beyond the command's own deadline, so the
# inner timeout (which kills the process group) fires first and the outer
# wait_for stays a backstop rather than the primary mechanism.
TIMEOUT_GRACE = 15.0

# Maximum SSH operations allowed to run concurrently across ALL monitors.
#
# Every polling cycle fires each monitor's SSH work at once. Without a bound,
# a wall of slow or unreachable hosts holds a blocking thread each until their
# timeout elapses, exhausting the interpreter's default thread-pool executor —
# which NiceGUI also uses — and freezing the web dashboard. A dedicated,
# bounded pool + semaphore keeps SSH from starving the rest of the app so the
# UI stays responsive even when many targets are down.
MAX_CONCURRENT_SSH = 16

_ssh_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SSH, thread_name_prefix="vigil-ssh")
_ssh_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SSH)


class SSHCollector:
    """
    Internal plugin for SSH-based data collection.
    Provides high-level methods to fetch raw data from remote targets.
    """
    def __init__(self, ssh_conn: SSHConnection, timeout: float = TIMEOUT):
        self.ssh = ssh_conn
        self.timeout = timeout

    async def fetch_output(self, command: str,
                           timeout: Optional[float] = None) -> Tuple[int, str, str]:
        """
        Execute a collection command off the event loop and return its results.

        `timeout` overrides the collector's default for one call — used where a
        single monitor knows some of its commands are slower than others.
        """
        deadline = timeout if timeout is not None else self.timeout
        try:
            loop = asyncio.get_event_loop()
            # Bound concurrency (semaphore) and run on a dedicated pool so a
            # backlog of slow/unreachable hosts cannot exhaust the default
            # executor that NiceGUI relies on.
            #
            # Two deadlines, deliberately: the inner one (passed to execute)
            # is what actually kills the ssh process group, while wait_for only
            # stops awaiting — the worker thread runs on regardless. The outer
            # one is a grace period longer so the inner kill normally wins and
            # nothing is orphaned; wait_for is just a backstop for a thread
            # wedged before its own timeout can fire.
            async with _ssh_semaphore:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        _ssh_executor,
                        functools.partial(self.ssh.execute, command, timeout=deadline),
                    ),
                    timeout=deadline + TIMEOUT_GRACE,
                )
        except asyncio.TimeoutError:
            logging.error(
                f"SSH command timed out after {deadline}s on {self.ssh.host}: {command!r}"
            )
            return -1, "", f"Timed out after {deadline}s"
        except Exception as e:
            logging.error(f"SSH Collector failed on {self.ssh.host}: {e}")
            return -1, "", str(e)