import shlex
import time
import uuid
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult

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


class Mosquitto(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.host = config.get('host', '127.0.0.1')
        self.port = int(config.get('port', 1883))
        self.username = config.get('username')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.probe_topic = config.get('probe_topic', f'vigil/probe/{self.id}')
        self.probe_timeout = int(config.get('probe_timeout', 5))
        self._started: Optional[float] = None

    def commands(self) -> List[Command]:
        self._started = time.monotonic()
        script = _build_probe_script(
            self.host, self.port, self.probe_topic, self.probe_timeout,
            self.username, self.password_command, self.password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        elapsed_ms = (time.monotonic() - self._started) * 1000.0 if self._started is not None else 0.0

        if ret != 0:
            if _TIMED_OUT in stderr:
                message = (
                    f"Publish/subscribe round trip on {self.probe_topic!r} timed out "
                    f"after {self.probe_timeout}s — broker accepted the connection but "
                    "did not deliver the message"
                )
            elif _MISMATCH in stderr:
                message = (
                    f"Publish/subscribe round trip returned an unexpected payload: "
                    f"{stderr.strip()}"
                )
            else:
                message = f"Failed to run MQTT round trip: {stderr.strip()}"
            return CollectResult(metrics={'roundtrip_ok': 0.0}, logs=[(message, "ERROR")], status='failed')

        return CollectResult(
            metrics={'roundtrip_ok': 1.0, 'roundtrip_ms': elapsed_ms},
            logs=[(f"Round trip OK on {self.probe_topic} ({elapsed_ms:.0f}ms)", "INFO")],
            status='online',
        )

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
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.spec import register_formatter, register_color_rule


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
