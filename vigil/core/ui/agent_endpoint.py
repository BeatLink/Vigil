"""WebSocket endpoint agents dial into.

Mounted on the same FastAPI app (and therefore the same port) as the
dashboard and REST API, so deploying an agent needs no extra listener,
certificate, or firewall rule beyond what the dashboard already uses.

The dashboard's HTTP Basic middleware does not apply here — it is a
``BaseHTTPMiddleware``, which Starlette runs only for ``http`` scopes — so
this endpoint does its own authentication: the agent presents its id and the
shared token declared for that id in ``agents:``, compared in constant time.
"""

import logging
import time
from typing import Any

from vigil.core.connectors import agent_protocol as proto
from vigil.core.connectors.agent_connector import AgentRegistry


def _tokens_match(given: str, expected: str) -> bool:
    import hmac
    return hmac.compare_digest(given, expected)


def register_agent_endpoint(app: Any, registry: AgentRegistry) -> None:
    from fastapi import WebSocket, WebSocketDisconnect

    @app.websocket('/api/agent/ws')
    async def agent_socket(websocket: WebSocket):
        await websocket.accept()
        conn = None
        try:
            hello = proto.decode(await websocket.receive_text())
            if hello.get('t') != proto.HELLO:
                await websocket.close(code=1002, reason='Expected a hello frame')
                return

            agent_id = str(hello.get('agent') or '')
            expected = registry.token_for(agent_id)
            given = str(hello.get('token') or '')
            if not agent_id or not expected or not _tokens_match(given, expected):
                logging.warning(
                    f"agent endpoint: rejected connection claiming id {agent_id!r} "
                    f"from {websocket.client.host if websocket.client else 'unknown'}"
                )
                await websocket.close(code=1008, reason='Authentication failed')
                return

            conn = registry.require(agent_id)
            conn.attach(websocket, hello)
            await websocket.send_text(proto.encode(
                proto.welcome([s.to_wire() for s in conn.streams])
            ))
            # Only now is this agent's transport usable, so this is the point
            # at which its monitors can meaningfully collect.
            registry.notify_connected(agent_id)

            await _serve(websocket, registry, conn, agent_id)
        except WebSocketDisconnect:
            pass
        # One misbehaving connection (bad frame, mid-handshake drop) must not take the endpoint down.
        except Exception as e:
            logging.warning(f"agent endpoint: connection ended with an error: {e}")
        finally:
            if conn is not None:
                conn.detach(websocket)


async def _serve(websocket: Any, registry: AgentRegistry, conn: Any, agent_id: str) -> None:
    """The per-connection receive loop. Every frame is dispatched by tag;
    unknown tags are ignored so a newer agent can send frames this server does
    not implement without breaking the link."""
    while True:
        frame = proto.decode(await websocket.receive_text())
        tag = frame.get('t')

        if tag == proto.RESULT:
            request_id = frame.get('id')
            if isinstance(request_id, int):
                conn.resolve(
                    request_id,
                    int(frame.get('rc', -1)),
                    str(frame.get('out', '')),
                    str(frame.get('err', '')),
                )
        elif tag == proto.EVENT:
            payload = frame.get('payload')
            registry.dispatch_event(
                agent_id,
                str(frame.get('stream') or ''),
                float(frame.get('ts') or time.time()),
                payload if isinstance(payload, dict) else {},
            )
        elif tag == proto.PING:
            await websocket.send_text(proto.encode({'t': proto.PONG, 'id': frame.get('id')}))
