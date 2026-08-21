"""Wire protocol shared by the Vigil server and the Vigil agent.

It lives in the agent package, not the server one, because the agent is the
constrained side: a monitored host installs ``vigil-agent`` and must not pull
in the dashboard's dependency tree to do it. The server imports this same
module, so there is exactly one definition of the wire format rather than two
copies to keep in step.

One JSON object per WebSocket text frame, discriminated by its ``t`` field.
The agent always dials outward to the server, so the server never needs a
route to the monitored host and the host never opens a listening port.

Two channels share the one socket:

* **Exec** — a request/response RPC (``exec`` / ``result``) that carries the
  same shell command strings the SSH connector used to send. This is what
  lets all existing plugins run over an agent without modification.
* **Events** — an agent-initiated push (``subscribe`` / ``event``) where the
  agent watches a source locally and sends a frame the moment something
  happens, with no poll interval involved.

Frames are deliberately small and self-describing rather than positional, so
an older agent talking to a newer server (or the reverse) can ignore fields
it does not know instead of failing to parse.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1

# --- Frame type tags ---

HELLO = "hello"
"""Agent -> server, first frame after connect: identity and capabilities."""

WELCOME = "welcome"
"""Server -> agent, accepting the hello and carrying the stream subscriptions."""

EXEC = "exec"
"""Server -> agent: run one shell command, reply with a RESULT of the same id."""

RESULT = "result"
"""Agent -> server: the outcome of one EXEC."""

SUBSCRIBE = "subscribe"
"""Server -> agent: the full set of event streams to watch from now on."""

EVENT = "event"
"""Agent -> server: something happened on a subscribed stream, right now."""

PING = "ping"
PONG = "pong"


def encode(frame: Dict[str, Any]) -> str:
    return json.dumps(frame, separators=(',', ':'))


def decode(raw: str) -> Dict[str, Any]:
    """Parse one frame. Returns {} for anything unparseable or not an object,
    so a malformed frame is dropped by the caller's tag check rather than
    tearing down an otherwise healthy connection."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Frame builders. Kept as functions so both sides agree on field names
# without either importing the other's module. ---

def hello(agent_id: str, hostname: str, version: str,
          capabilities: List[str]) -> Dict[str, Any]:
    return {
        't': HELLO,
        'protocol': PROTOCOL_VERSION,
        'agent': agent_id,
        'hostname': hostname,
        'version': version,
        'caps': capabilities,
    }


def welcome(streams: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {'t': WELCOME, 'protocol': PROTOCOL_VERSION, 'streams': streams}


def exec_request(request_id: int, command: str, timeout: float) -> Dict[str, Any]:
    return {'t': EXEC, 'id': request_id, 'cmd': command, 'timeout': timeout}


def exec_result(request_id: int, exit_code: int, stdout: str, stderr: str) -> Dict[str, Any]:
    return {'t': RESULT, 'id': request_id, 'rc': exit_code, 'out': stdout, 'err': stderr}


def subscribe(streams: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {'t': SUBSCRIBE, 'streams': streams}


def event(stream_id: str, payload: Dict[str, Any], timestamp: float) -> Dict[str, Any]:
    return {'t': EVENT, 'stream': stream_id, 'ts': timestamp, 'payload': payload}


@dataclass(frozen=True)
class StreamSpec:
    """One event source an agent should watch. ``kind`` selects the agent-side
    watcher implementation; ``params`` is that watcher's own config, passed
    through opaquely so adding a watcher needs no protocol change.

    ``id`` is the owning monitor's plugin id, which is how an inbound EVENT
    frame is routed back to the plugin that asked for it."""
    id: str
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> Dict[str, Any]:
        return {'id': self.id, 'kind': self.kind, 'params': dict(self.params)}

    @staticmethod
    def from_wire(raw: Dict[str, Any]) -> Optional["StreamSpec"]:
        stream_id, kind = raw.get('id'), raw.get('kind')
        if not stream_id or not kind:
            return None
        params = raw.get('params')
        return StreamSpec(str(stream_id), str(kind),
                          params if isinstance(params, dict) else {})
