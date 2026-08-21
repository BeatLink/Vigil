"""End-to-end agent link: a real AgentClient over a real WebSocket to the
real endpoint. Covers the parts a mocked socket cannot — authentication, the
hello/welcome handshake, exec round-trips, and a watcher's event reaching the
server's sink."""

import asyncio
import contextlib
import socket

import pytest

from vigil.core.connectors.agent_connector import AgentRegistry
from vigil.core.ui.agent_endpoint import register_agent_endpoint
from vigil_agent.client import AgentClient
from vigil_agent.protocol import StreamSpec

pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _server(registry: AgentRegistry):
    import uvicorn
    from fastapi import FastAPI

    app = FastAPI()
    register_agent_endpoint(app, registry)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='error'))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("uvicorn did not start")

    try:
        yield f"ws://127.0.0.1:{port}/api/agent/ws"
    finally:
        server.should_exit = True
        await task


@contextlib.asynccontextmanager
async def _agent(url, agent_id='node-a', token='secret'):
    client = AgentClient(url, agent_id, token)
    task = asyncio.create_task(client.run_forever())
    try:
        yield client
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _await_connected(conn, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if conn.is_connected:
            return
        await asyncio.sleep(0.05)
    pytest.fail("the agent never connected")


def _registry(**overrides):
    registry = AgentRegistry()
    registry.configure([{'id': 'node-a', 'token': 'secret', 'host': 'node-a.lan', **overrides}])
    return registry


class TestHandshake:
    async def test_a_valid_token_connects(self):
        registry = _registry()
        async with _server(registry) as url:
            async with _agent(url):
                await _await_connected(registry.get('node-a'))
                assert registry.get('node-a').version is not None

    async def test_a_wrong_token_is_refused(self):
        registry = _registry()
        async with _server(registry) as url:
            async with _agent(url, token='wrong'):
                await asyncio.sleep(0.5)
                assert registry.get('node-a').is_connected is False


class TestExecOverTheWire:
    async def test_a_command_round_trips(self):
        registry = _registry()
        async with _server(registry) as url:
            async with _agent(url):
                conn = registry.get('node-a')
                await _await_connected(conn)
                assert await conn.execute("echo hello", timeout=5) == (0, "hello\n", "")

    async def test_a_slow_command_does_not_block_a_fast_one(self):
        """The session-ceiling difference from SSH, over a real socket."""
        registry = _registry()
        async with _server(registry) as url:
            async with _agent(url):
                conn = registry.get('node-a')
                await _await_connected(conn)

                slow = asyncio.create_task(conn.execute("sleep 1; echo slow", timeout=10))
                fast = await asyncio.wait_for(conn.execute("echo fast", timeout=5), timeout=2)
                assert fast[1] == "fast\n"
                assert (await slow)[1] == "slow\n"


class TestEventsOverTheWire:
    async def test_a_watcher_event_reaches_the_server(self):
        registry = _registry()
        received = asyncio.Queue()
        registry.set_event_sink(
            lambda agent_id, stream_id, ts, payload: received.put_nowait((stream_id, payload))
        )
        registry.get('node-a').register_stream(StreamSpec(
            'mon-1', 'sample', {'command': 'echo tick', 'interval': 0.1},
        ))

        async with _server(registry) as url:
            async with _agent(url):
                await _await_connected(registry.get('node-a'))
                stream_id, payload = await asyncio.wait_for(received.get(), timeout=5)

        assert stream_id == 'mon-1'
        assert payload['stdout'] == "tick\n"

    async def test_subscriptions_are_pushed_to_a_live_agent(self):
        registry = _registry()
        received = asyncio.Queue()
        registry.set_event_sink(
            lambda agent_id, stream_id, ts, payload: received.put_nowait(stream_id)
        )

        async with _server(registry) as url:
            async with _agent(url):
                conn = registry.get('node-a')
                await _await_connected(conn)

                # Added after the handshake: the agent must pick it up from a
                # subscribe frame, not only from the welcome.
                conn.register_stream(StreamSpec(
                    'late-1', 'sample', {'command': 'echo late', 'interval': 0.1},
                ))
                await conn.push_subscriptions()
                assert await asyncio.wait_for(received.get(), timeout=5) == 'late-1'
