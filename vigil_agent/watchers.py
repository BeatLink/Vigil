"""Event watchers — the agent's push side.

A watcher runs locally on the monitored host, notices something, and hands a
payload to the agent to send immediately. This is what a polled transport
cannot do: the server learns about a state change when it happens rather than
on the next cycle, and a watcher that samples does so locally, where a read of
``/proc`` costs microseconds instead of an SSH round trip.

Each watcher is one long-lived coroutine created from a ``StreamSpec``; the
agent cancels and rebuilds them whenever the server sends a new subscription
set. A watcher that raises is logged and restarted with backoff rather than
taking the connection down with it.

Adding a watcher means adding one coroutine and one ``WATCHERS`` entry — the
wire protocol passes ``params`` through opaquely, so neither side needs a
protocol change.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

Emit = Callable[[Dict[str, Any]], Awaitable[None]]
"""Send one event payload to the server, now."""

_RESTART_BACKOFF_SECONDS = 5.0


async def journal(params: Dict[str, Any], emit: Emit) -> None:
    """Follow the systemd journal and push each matching entry as it is
    written. The canonical event source on a systemd host: unit state changes,
    OOM kills, and service failures all land here the instant they occur.

    params: unit (str), priority (0-7), grep (substring), identifier (str).
    """
    cmd = ['journalctl', '--follow', '--lines=0', '--output=json']
    if params.get('unit'):
        cmd += ['--unit', str(params['unit'])]
    if params.get('identifier'):
        cmd += ['--identifier', str(params['identifier'])]
    if params.get('priority') is not None:
        cmd += ['--priority', str(params['priority'])]
    if params.get('kernel'):
        cmd += ['--dmesg']

    needle = params.get('grep')
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("journalctl --follow exited")
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = str(entry.get('MESSAGE', ''))
            if needle and needle not in message:
                continue
            await emit({
                'message': message,
                'unit': entry.get('_SYSTEMD_UNIT') or entry.get('UNIT'),
                'priority': entry.get('PRIORITY'),
                'pid': entry.get('_PID'),
                'identifier': entry.get('SYSLOG_IDENTIFIER'),
            })
    finally:
        if proc.returncode is None:
            proc.terminate()


async def path(params: Dict[str, Any], emit: Emit) -> None:
    """Watch a filesystem path and push when its mtime or size changes.

    Sampling, but sampling *locally* — a stat() is cheap enough to run every
    quarter second, which is a resolution no remote poller can reach.

    params: path (str, required), interval (float seconds, default 0.25).
    """
    target = params.get('path')
    if not target:
        raise ValueError("path watcher requires a `path` param")
    period = float(params.get('interval', 0.25))
    previous: Optional[tuple] = None

    while True:
        try:
            info = os.stat(target)
            current = (info.st_mtime, info.st_size)
            exists = True
        except OSError:
            current, exists = None, False

        if previous is not None and current != previous:
            await emit({
                'path': target,
                'exists': exists,
                'mtime': current[0] if current else None,
                'size': current[1] if current else None,
            })
        previous = current
        await asyncio.sleep(period)


async def sample(params: Dict[str, Any], emit: Emit) -> None:
    """Run a command locally on a fast interval and push its output, either
    every time or only when it changes.

    This is the high-resolution path: a one-second CPU or latency sample costs
    a local fork, not an SSH channel, so a monitor can have per-second
    resolution without the target's sshd ever seeing it.

    params: command (str, required), interval (float, default 1.0),
            on_change (bool, default False).
    """
    from vigil_agent import executor

    command = params.get('command')
    if not command:
        raise ValueError("sample watcher requires a `command` param")
    period = float(params.get('interval', 1.0))
    only_on_change = bool(params.get('on_change', False))
    previous: Optional[str] = None

    while True:
        code, out, err = await executor.run(command, timeout=max(period, 5.0))
        if not (only_on_change and out == previous):
            await emit({'exit_code': code, 'stdout': out, 'stderr': err})
        previous = out
        await asyncio.sleep(period)


WATCHERS: Dict[str, Callable[[Dict[str, Any], Emit], Awaitable[None]]] = {
    'journal': journal,
    'path': path,
    'sample': sample,
}


async def supervise(kind: str, stream_id: str, params: Dict[str, Any],
                    emit: Emit) -> None:
    """Run one watcher forever, restarting it after a failure. Cancellation
    (the server changed the subscription set, or the agent is shutting down)
    propagates instead of being retried."""
    watcher = WATCHERS.get(kind)
    if watcher is None:
        logging.error(f"stream {stream_id!r}: unknown watcher kind {kind!r}")
        return

    async def _emit(payload: Dict[str, Any]) -> None:
        await emit({'stream': stream_id, 'ts': time.time(), 'payload': payload})

    while True:
        try:
            await watcher(params, _emit)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"stream {stream_id!r} ({kind}) failed: {e}; restarting")
        await asyncio.sleep(_RESTART_BACKOFF_SECONDS)
