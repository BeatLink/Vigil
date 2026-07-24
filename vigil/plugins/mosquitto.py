import shlex
import uuid
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_TIMED_OUT = "VIGIL_MQTT_TIMEOUT"
_MISMATCH = "VIGIL_MQTT_MISMATCH"


def _auth_flags(username: Optional[str], password_command: Optional[str],
                password: Optional[str]) -> str:
    if not username:
        return ''
    if password_command:
        return f'-u {shlex.quote(username)} -P "$__pw"'
    if password:
        return f'-u {shlex.quote(username)} -P {shlex.quote(password)}'
    return f'-u {shlex.quote(username)}'


def _build_probe_script(host: str, port: int, topic: str, timeout: int,
                        username: Optional[str], password_command: Optional[str],
                        password: Optional[str]) -> str:
    lines = ["set -e"]

    if password_command:
        lines.append(f"__pw=$({password_command})")

    auth = _auth_flags(username, password_command, password)
    nonce = f"vigil-probe-{uuid.uuid4().hex}"

    lines.append(f'__nonce={shlex.quote(nonce)}')
    lines.append(
        f'__got=$(timeout {int(timeout)} mosquitto_sub -h {shlex.quote(host)} '
        f'-p {int(port)} -t {shlex.quote(topic)} -C 1 -W {int(timeout)} {auth} & '
        f'__sub_pid=$!; '
        f'sleep 0.3; '
        f'mosquitto_pub -h {shlex.quote(host)} -p {int(port)} '
        f'-t {shlex.quote(topic)} -m "$__nonce" {auth}; '
        f'wait "$__sub_pid")'
    )
    lines.append(
        f'if [ -z "$__got" ]; then echo "{_TIMED_OUT}" >&2; exit 1; fi'
    )
    lines.append(
        f'if [ "$__got" != "$__nonce" ]; then '
        f'echo "{_MISMATCH}: expected $__nonce, got $__got" >&2; exit 1; fi'
    )
    lines.append('echo "$__nonce"')
    return '\n'.join(lines)


_DEFAULT_LAYOUT = [
    ['host_card', 'roundtrip_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class MosquittoCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.host = config.get('host', '127.0.0.1')
        self.port = int(config.get('port', 1883))
        self.username = config.get('username')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.probe_topic = config.get('probe_topic', f'vigil/probe/{self.id}')
        self.probe_timeout = int(config.get('probe_timeout', 5))

    async def on_collect(self):
        script = _build_probe_script(
            self.host, self.port, self.probe_topic, self.probe_timeout,
            self.username, self.password_command, self.password,
        )
        import time as _time
        started = _time.monotonic()
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        elapsed_ms = (_time.monotonic() - started) * 1000.0

        if ret != 0:
            if _TIMED_OUT in stderr:
                self.db_logger.write(
                    f"Publish/subscribe round trip on {self.probe_topic!r} timed out "
                    f"after {self.probe_timeout}s — broker accepted the connection but "
                    "did not deliver the message", level="ERROR")
            elif _MISMATCH in stderr:
                self.db_logger.write(
                    f"Publish/subscribe round trip returned an unexpected payload: "
                    f"{stderr.strip()}", level="ERROR")
            else:
                self.db_logger.write(
                    f"Failed to run MQTT round trip: {stderr.strip()}", level="ERROR")
            self.db_metrics.metric('roundtrip_ok', 0.0)
            self.set_status('failed')
            return

        self.db_metrics.metric('roundtrip_ok', 1.0)
        self.db_metrics.metric('roundtrip_ms', elapsed_ms)
        self.db_logger.write(
            f"Round trip OK on {self.probe_topic} ({elapsed_ms:.0f}ms)", level="INFO")
        self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class MosquittoUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'roundtrip_card': {
                'metric': 'roundtrip_ok', 'title': 'ROUND TRIP',
                'format': 'mosquitto_ok_text', 'color': 'mosquitto_ok_color',
            },
            'latency_card': {'metric': 'roundtrip_ms', 'title': 'LATENCY', 'format': 'ms0'},
        },
        'chart': {'metric': 'roundtrip_ms', 'title': 'ROUND TRIP LATENCY (ms)'},
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter, register_color_rule


@register_formatter('mosquitto_ok_text')
def _roundtrip_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('mosquitto_ok_color')
def _roundtrip_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
