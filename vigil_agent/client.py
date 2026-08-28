"""The agent's connection to the Vigil server.

One outbound WebSocket, held open. The agent reconnects on its own with
exponential backoff, so a server restart or a flapping link needs no operator
action and the monitors it feeds recover by themselves.

Commands are dispatched into their own tasks rather than handled inline: a
thirty-second `borg` check must not stop the agent answering a one-second CPU
sample on the same socket. That is the concrete difference from SSH, where
concurrency was bounded by the target's ``MaxSessions``; here the only limit
is what the host can usefully run at once.
"""

import asyncio
import logging
import random
import socket
from typing import Any, Dict, List, Optional

import websockets

from vigil_agent import __version__, executor, protocol as proto, watchers

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0

CAPABILITIES = sorted(watchers.WATCHERS)
"""What this agent can do, sent in the hello so a server can tell an older
agent apart from a newer one without a version comparison."""


class AgentClient:
    def __init__(self, url: str, agent_id: str, token: str,
                 hostname: Optional[str] = None):
        self.url = url
        self.agent_id = agent_id
        self.token = token
        self.hostname = hostname or socket.gethostname()
        self._socket: Any = None
        self._send_lock = asyncio.Lock()
        self._streams: Dict[str, asyncio.Task] = {}

    # --- Connection lifecycle ---

    async def run_forever(self) -> None:
        """Dial, serve, and redial. Only cancellation ends this."""
        backoff = _BACKOFF_INITIAL
        while True:
            try:
                await self._session()
                backoff = _BACKOFF_INITIAL
            except asyncio.CancelledError:
                await self._stop_streams()
                raise
            # Any session failure becomes a redial; a network blip must not end the agent.
            except Exception as e:
                logging.warning(f"connection to {self.url} ended: {e}")

            # Jitter keeps a fleet of agents from redialling a restarted
            # server in lockstep.
            delay = min(backoff, _BACKOFF_MAX) * random.uniform(0.5, 1.5)
            logging.info(f"reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _session(self) -> None:
        """One connection, from hello to disconnect."""
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as socket_:
            self._socket = socket_
            hello = proto.hello(self.agent_id, self.hostname, __version__, CAPABILITIES)
            hello['token'] = self.token
            await socket_.send(proto.encode(hello))

            welcome = proto.decode(await socket_.recv())
            if welcome.get('t') != proto.WELCOME:
                raise ConnectionError("server did not accept the agent (check the token)")
            logging.info(f"connected to {self.url} as {self.agent_id!r}")

            await self._apply_streams(welcome.get('streams') or [])
            try:
                await self._receive_loop(socket_)
            finally:
                self._socket = None
                await self._stop_streams()

    async def _receive_loop(self, socket_: Any) -> None:
        async for raw in socket_:
            frame = proto.decode(raw)
            tag = frame.get('t')
            if tag == proto.EXEC:
                # Its own task: a long command must not block the socket.
                asyncio.create_task(self._handle_exec(frame))
            elif tag == proto.SUBSCRIBE:
                await self._apply_streams(frame.get('streams') or [])
            elif tag == proto.PING:
                await self._send({'t': proto.PONG, 'id': frame.get('id')})

    # --- Exec ---

    async def _handle_exec(self, frame: Dict[str, Any]) -> None:
        request_id = frame.get('id')
        if not isinstance(request_id, int):
            return
        command = str(frame.get('cmd', ''))
        timeout = float(frame.get('timeout') or executor.DEFAULT_TIMEOUT)
        code, out, err = await executor.run(command, timeout=timeout)
        try:
            await self._send(proto.exec_result(request_id, code, out, err))
        except Exception as e:
            # The socket died while the command ran; the server has already
            # failed the call, so dropping the reply is the correct outcome.
            logging.debug(f"could not deliver result for request {request_id}: {e}")

    # --- Streams ---

    async def _apply_streams(self, raw_streams: List[Dict[str, Any]]) -> None:
        """Converge on the server's subscription set: cancel what is gone,
        start what is new, leave unchanged streams running so a reconnect does
        not restart a healthy journal follow."""
        specs = [s for s in (proto.StreamSpec.from_wire(r) for r in raw_streams) if s]
        wanted = {s.id: s for s in specs}

        for stream_id in list(self._streams):
            if stream_id not in wanted:
                await self._cancel_stream(stream_id)

        for stream_id, spec in wanted.items():
            if stream_id in self._streams:
                continue
            self._streams[stream_id] = asyncio.create_task(
                watchers.supervise(spec.kind, spec.id, spec.params, self._emit_event)
            )
            logging.info(f"watching stream {stream_id!r} ({spec.kind})")

    async def _emit_event(self, envelope: Dict[str, Any]) -> None:
        await self._send(proto.event(
            envelope['stream'], envelope['payload'], envelope['ts']
        ))

    async def _cancel_stream(self, stream_id: str) -> None:
        task = self._streams.pop(stream_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logging.info(f"stopped watching stream {stream_id!r}")

    async def _stop_streams(self) -> None:
        for stream_id in list(self._streams):
            await self._cancel_stream(stream_id)

    # --- Send ---

    async def _send(self, frame: Dict[str, Any]) -> None:
        socket_ = self._socket
        if socket_ is None:
            raise ConnectionError("not connected")
        async with self._send_lock:
            await socket_.send(proto.encode(frame))
