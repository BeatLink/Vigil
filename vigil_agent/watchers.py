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
import itertools
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

Emit = Callable[[Dict[str, Any]], Awaitable[None]]
"""Send one event payload to the server, now."""

_RESTART_BACKOFF_SECONDS = 5.0
_JOURNAL_SETTLE_SECONDS = 0.2

_UNIT_FIELDS = ('_SYSTEMD_UNIT', 'UNIT', 'OBJECT_SYSTEMD_UNIT', 'COREDUMP_UNIT')


def _mangle_unit(name: str) -> str:
    """Append .service to a bare unit name, as journalctl itself does."""
    return name if '.' in name else f'{name}.service'


def _entry_matches(params: Dict[str, Any], entry: Dict[str, Any],
                   message: str) -> bool:
    """Whether one journal entry satisfies a stream's filter params."""
    unit = params.get('unit')
    if unit and _mangle_unit(str(unit)) not in (entry.get(f) for f in _UNIT_FIELDS):
        return False
    identifier = params.get('identifier')
    if identifier and entry.get('SYSLOG_IDENTIFIER') != str(identifier):
        return False
    if params.get('priority') is not None:
        try:
            if int(entry.get('PRIORITY')) > int(params['priority']):
                return False
        except (TypeError, ValueError):
            return False
    needle = params.get('grep')
    if needle and needle not in message:
        return False
    return True


class _JournalSub:
    """One stream's registration with the mux: its params and delivery queue."""

    def __init__(self, params: Dict[str, Any]):
        self.params = params
        self.queue: asyncio.Queue = asyncio.Queue()


class _JournalGroup:
    """One shared journalctl follower serving every stream in the group."""

    def __init__(self, key: Any):
        self.key = key
        self.subs: Set[_JournalSub] = set()
        self.task: Optional[asyncio.Task] = None

    def add(self, sub: _JournalSub) -> None:
        self.subs.add(sub)
        self._restart()

    def remove(self, sub: _JournalSub) -> None:
        self.subs.discard(sub)
        self._restart()

    def _restart(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        if self.subs:
            self.task = asyncio.create_task(self._run())

    def _command(self) -> List[str]:
        cmd = ['journalctl', '--follow', '--lines=0', '--output=json']
        if self.key == 'unit':
            for unit in sorted({str(s.params['unit']) for s in self.subs}):
                cmd += ['--unit', unit]
        elif self.key == 'identifier':
            for ident in sorted({str(s.params['identifier']) for s in self.subs}):
                cmd += ['--identifier', ident]
        elif self.key == 'kernel':
            cmd += ['--dmesg']
        else:
            only = next(iter(self.subs))
            if only.params.get('priority') is not None:
                cmd += ['--priority', str(only.params['priority'])]
        return cmd

    async def _spawn(self, cmd: List[str]) -> Any:
        return await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    async def _run(self) -> None:
        proc = None
        try:
            # Settle briefly so a welcome frame's burst of registrations builds one process, not one per stream.
            await asyncio.sleep(_JOURNAL_SETTLE_SECONDS)
            proc = await self._spawn(self._command())
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._route(line)
        except asyncio.CancelledError:
            raise
        # A dying journalctl must not crash the agent; _fail() below tells subscribers to restart.
        except Exception as e:
            logging.warning(f"journal follower {self.key!r} failed: {e}")
        finally:
            if proc is not None and proc.returncode is None:
                proc.terminate()
        self._fail()

    def _route(self, line: bytes) -> None:
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        message = str(entry.get('MESSAGE', ''))
        for sub in list(self.subs):
            if _entry_matches(sub.params, entry, message):
                sub.queue.put_nowait({
                    'message': message,
                    'unit': entry.get('_SYSTEMD_UNIT') or entry.get('UNIT'),
                    'priority': entry.get('PRIORITY'),
                    'pid': entry.get('_PID'),
                    'identifier': entry.get('SYSLOG_IDENTIFIER'),
                })

    def _fail(self) -> None:
        for sub in list(self.subs):
            sub.queue.put_nowait(None)


class JournalMux:
    """Routes journal streams onto shared journalctl followers.

    Streams that filter by unit ride one process carrying the union of their
    ``--unit`` flags, kernel streams share one ``--dmesg`` process, and
    identifier-only streams share one ``--identifier`` union; each stream's
    own filters (unit, identifier, priority, grep) are applied while routing
    the JSON entries, so every stream sees exactly the entries its own
    journalctl invocation would have, in the same payload shape. A membership
    change restarts the shared process, which can drop lines written during
    the brief gap.
    """

    def __init__(self):
        self._groups: Dict[Any, _JournalGroup] = {}
        self._solo = itertools.count()

    def _key(self, params: Dict[str, Any]) -> Any:
        if params.get('unit'):
            return 'unit'
        if params.get('kernel'):
            return 'kernel'
        if params.get('identifier'):
            return 'identifier'
        return ('solo', next(self._solo))

    async def follow(self, params: Dict[str, Any], emit: Emit) -> None:
        sub = _JournalSub(params)
        key = self._key(params)
        group = self._groups.setdefault(key, _JournalGroup(key))
        group.add(sub)
        try:
            while True:
                payload = await sub.queue.get()
                if payload is None:
                    raise RuntimeError("journalctl --follow exited")
                await emit(payload)
        finally:
            group.remove(sub)
            if not group.subs:
                self._groups.pop(key, None)


_JOURNAL_MUX = JournalMux()


async def journal(params: Dict[str, Any], emit: Emit) -> None:
    """Follow the systemd journal and push each matching entry as it is
    written. The canonical event source on a systemd host: unit state changes,
    OOM kills, and service failures all land here the instant they occur.
    Streams share journalctl processes through the mux, so N unit monitors
    cost one follower rather than N.

    params: unit (str), priority (0-7), grep (substring), identifier (str),
    kernel (bool).
    """
    await _JOURNAL_MUX.follow(params, emit)


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
            on_change (bool, default False), max_quiet (float seconds).
    ``max_quiet`` suppresses unchanged results like ``on_change`` but still
    pushes one at least every max_quiet seconds, so a monitor whose sample is
    its whole collection can never read as stale on the server.
    """
    from vigil_agent import executor

    command = params.get('command')
    if not command:
        raise ValueError("sample watcher requires a `command` param")
    period = float(params.get('interval', 1.0))
    max_quiet = params.get('max_quiet')
    suppress_unchanged = bool(params.get('on_change', False)) or max_quiet is not None
    previous: Optional[tuple] = None
    last_emit = 0.0

    while True:
        code, out, err = await executor.run(command, timeout=max(period, 5.0))
        current = (code, out, err)
        overdue = max_quiet is not None and time.monotonic() - last_emit >= float(max_quiet)
        if not suppress_unchanged or current != previous or overdue:
            await emit({'exit_code': code, 'stdout': out, 'stderr': err})
            last_emit = time.monotonic()
        previous = current
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
