import json
import shlex
from typing import Any, Dict, Optional, Tuple

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

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


class OpenbooksCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.ws_url = config.get('ws_url', 'ws://127.0.0.1:9777/ws')
        self.probe_timeout = int(config.get('probe_timeout', 8))

    async def on_collect(self):
        script = _build_probe_script(self.ws_url, self.probe_timeout)
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(
                f"WebSocket probe failed: {stderr.strip() or 'timed out or connection refused'}",
                level="ERROR")
            self.db_metrics.metric('bridge_connected', 0.0)
            self.set_status('failed')
            return

        try:
            msg_type, appearance = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.db_metrics.metric('bridge_connected', 0.0)
            self.set_status('failed')
            return

        connected = msg_type == _MSG_TYPE_CONNECT and appearance == _APPEARANCE_SUCCESS
        self.db_metrics.metric('bridge_connected', 1.0 if connected else 0.0)

        if connected:
            self.db_logger.write("IRC bridge connected", level="INFO")
            self.set_status('online')
            return

        if msg_type == _MSG_TYPE_STATUS and appearance == _APPEARANCE_DANGER:
            self.db_logger.write(
                "IRC bridge reported a connection failure", level="ERROR")
        else:
            self.db_logger.write(
                f"Unexpected response (type={msg_type}, appearance={appearance})",
                level="ERROR")
        self.set_status('failed')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class OpenbooksUIPlugin(UIPlugin):
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
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter, register_color_rule


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
