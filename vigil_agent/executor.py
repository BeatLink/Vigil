"""Local command execution.

The server sends the same shell command strings the SSH connector used to
send, so this returns the same ``(exit_code, stdout, stderr)`` triple and maps
every failure into it rather than raising.

Each command runs in its own session (``start_new_session=True``) so a timeout
can kill the whole process group. That closes a gap the SSH transport had: over
SSH, killing the remote process left anything it had spawned running, which is
how a wedged probe used to strand children on the target until reboot.
"""

import asyncio
import logging
import os
import signal
from typing import Tuple

DEFAULT_TIMEOUT = 30.0
KILL_GRACE_SECONDS = 5.0

_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
"""Cap on captured stdout/stderr per command. A runaway command must not be
able to push an unbounded frame at the server; the output is truncated with a
marker so the operator can see it happened."""


def _truncate(raw: bytes) -> str:
    text = raw[:_MAX_OUTPUT_BYTES].decode('utf-8', errors='replace')
    if len(raw) > _MAX_OUTPUT_BYTES:
        text += f"\n[vigil-agent: output truncated at {_MAX_OUTPUT_BYTES} bytes]"
    return text


async def run(command: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
    """Run one shell command, returning (exit_code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        return -1, "", f"Could not start command: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return -1, "", f"Timed out after {timeout}s"

    code = proc.returncode if proc.returncode is not None else -1
    return code, _truncate(stdout or b''), _truncate(stderr or b'')


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the command's process group, then SIGKILL what survives."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            continue
    logging.warning(f"could not reap pid {proc.pid} after SIGKILL (uninterruptible?)")
