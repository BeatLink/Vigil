import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple
from vigil.core.common.ssh_connector import SSHConnection

TIMEOUT = 30.0

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
    def __init__(self, ssh_conn: SSHConnection):
        self.ssh = ssh_conn

    async def fetch_output(self, command: str) -> Tuple[int, str, str]:
        """Executes a collection command off the event loop and returns the results."""
        try:
            loop = asyncio.get_event_loop()
            # Bound concurrency (semaphore) and run on a dedicated pool so a
            # backlog of slow/unreachable hosts cannot exhaust the default
            # executor that NiceGUI relies on. wait_for still enforces a
            # per-command deadline so a stuck host is abandoned, not hung on.
            async with _ssh_semaphore:
                return await asyncio.wait_for(
                    loop.run_in_executor(_ssh_executor, self.ssh.execute, command),
                    timeout=TIMEOUT
                )
        except asyncio.TimeoutError:
            logging.error(f"SSH command timed out after {TIMEOUT}s on {self.ssh.host}: {command!r}")
            return -1, "", f"Timed out after {TIMEOUT}s"
        except Exception as e:
            logging.error(f"SSH Collector failed on {self.ssh.host}: {e}")
            return -1, "", str(e)