"""
OpenBooks IRC-bridge health via a live WebSocket connect probe.

Complements a `systemd_service` monitor on openbooks rather than replacing
it. That one answers "is the web server alive"; this one answers "is the
IRC bridge to irc.irchighway.net actually connected", which is a different
failure — OpenBooks' HTTP server keeps serving its UI shell perfectly well
even when the underlying IRC client has failed to connect or been
disconnected, since the two are wired together only over the WebSocket, not
reflected in anything HTTP-visible.

OpenBooks has no REST health endpoint and, per its source, logs nothing
distinctive on a successful IRC join — the only place connection status
surfaces at all is the WebSocket protocol used by its own web UI: a client
sends a CONNECT request (`{"type": 1, "payload": {}}`) and the server
replies with a CONNECT response whose `payload.appearance` says whether the
IRC join succeeded. This plugin does exactly that and nothing else, then
disconnects immediately.

That immediacy matters for a reason specific to this app: OpenBooks serves
only one connected WebSocket client at a time (its stated design, not a bug)
— a monitor that stayed connected would occupy that slot and lock out real
use of the search UI. The probe therefore opens, sends one message, reads
one reply, and closes within the configured timeout, spending as little
time in that slot as a poll reasonably can.

Run over SSH via `websocat` (a WebSocket CLI client) rather than a Python
WebSocket library, matching how every other plugin's remote check runs a
CLI tool on the monitored host instead of connecting from Vigil's own
process.

Config options:
  ws_url        WebSocket URL, as seen from the monitored host (default:
                ws://127.0.0.1:9777/ws)
  probe_timeout Seconds allowed for the whole connect/reply/close round trip
                (default: 8 — the app's own connect flow includes a short
                internal delay, so this needs headroom beyond a bare ping)
"""
import json
import shlex
from typing import Any, Dict, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, on_data_event
from vigil.core.ui.theme import STATUS_COLORS

# OpenBooks' own message type/appearance enums (server/messages.go).
_MSG_TYPE_STATUS = 0
_MSG_TYPE_CONNECT = 1
_APPEARANCE_SUCCESS = 1
_APPEARANCE_DANGER = 3

_CONNECT_REQUEST = json.dumps({"type": _MSG_TYPE_CONNECT, "payload": {}})


def _build_probe_script(ws_url: str, timeout: int) -> str:
    """
    Build a shell script that sends one CONNECT request over websocat and
    prints the first reply. `-n1` closes the connection after one message
    each way rather than staying open, so the single-client slot is held
    for as little time as this round trip actually takes.
    """
    return (
        f'echo {shlex.quote(_CONNECT_REQUEST)} | '
        f'timeout {int(timeout)} websocat -n1 {shlex.quote(ws_url)}'
    )


def _parse_response(stdout: str) -> Tuple[int, int]:
    """Return (type, appearance) from the first JSON reply line."""
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


class OpenbooksPlugin(BasePlugin):
    """Monitors OpenBooks' IRC bridge connectivity via a WebSocket connect probe."""

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

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('bridge_card'):
            bridge_label = info_card('IRC BRIDGE', '--')
        with layout.cell('chart'):
            history_chart('IRC BRIDGE CONNECTED', self.id, 'bridge_connected')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            connected = self.latest_metric('bridge_connected')
            if connected:
                ok = connected.value >= 1.0
                bridge_label.text = 'CONNECTED' if ok else 'DISCONNECTED'
                bridge_label.style(
                    f'color: {STATUS_COLORS["online" if ok else "failed"]}')

        on_data_event('metric', bridge_label, update_cards)
