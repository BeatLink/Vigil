import json
import shlex
from typing import Any, Dict, List, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult

_MSG_TYPE_STATUS = 0
_MSG_TYPE_CONNECT = 1
_APPEARANCE_SUCCESS = 1
_APPEARANCE_DANGER = 3

_CONNECT_REQUEST = json.dumps({"type": _MSG_TYPE_CONNECT, "payload": {}})


def _build_probe_script(ws_url: str, timeout: int) -> str:
    return (
        f'echo {shlex.quote(_CONNECT_REQUEST)} | '
        f'timeout {int(timeout)} websocat -n1 {shlex.quote(ws_url)}'
    )


def _parse_response(stdout: str) -> Tuple[int, int]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = data.get('payload', {}) or {}
        return int(data.get('type', -1)), int(payload.get('appearance', -1))
    raise ValueError(f"no parseable message in response: {stdout[:200]!r}")


_DEFAULT_LAYOUT = [
    ['host_card', 'bridge_card'],
    ['chart'],
    ['events'],
]


class Openbooks(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.ws_url = config.get('ws_url', 'ws://127.0.0.1:9777/ws')
        self.probe_timeout = int(config.get('probe_timeout', 8))

    def commands(self) -> List[Command]:
        return [Command(_build_probe_script(self.ws_url, self.probe_timeout))]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult(
                metrics={'bridge_connected': 0.0},
                logs=[(
                    f"WebSocket probe failed: {stderr.strip() or 'timed out or connection refused'}",
                    "ERROR",
                )],
                status='failed',
            )

        try:
            msg_type, appearance = _parse_response(stdout)
        except ValueError as e:
            return CollectResult(
                metrics={'bridge_connected': 0.0},
                logs=[(str(e), "ERROR")],
                status='failed',
            )

        connected = msg_type == _MSG_TYPE_CONNECT and appearance == _APPEARANCE_SUCCESS
        metrics = {'bridge_connected': 1.0 if connected else 0.0}

        if connected:
            return CollectResult(
                metrics=metrics,
                logs=[("IRC bridge connected", "INFO")],
                status='online',
            )

        if msg_type == _MSG_TYPE_STATUS and appearance == _APPEARANCE_DANGER:
            message = "IRC bridge reported a connection failure"
        else:
            message = f"Unexpected response (type={msg_type}, appearance={appearance})"

        return CollectResult(
            metrics=metrics,
            logs=[(message, "ERROR")],
            status='failed',
        )

    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'bridge_card': {
                'metric': 'bridge_connected', 'title': 'IRC BRIDGE',
                'format': 'openbooks_bridge_text', 'color': 'openbooks_bridge_color',
            },
        },
        'chart': {'metric': 'bridge_connected', 'title': 'IRC BRIDGE CONNECTED'},
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.spec import register_formatter, register_color_rule


@register_formatter('openbooks_bridge_text')
def _bridge_text(v):
    if v is None:
        return '--'
    return 'CONNECTED' if v >= 1.0 else 'DISCONNECTED'


@register_color_rule('openbooks_bridge_color')
def _bridge_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
