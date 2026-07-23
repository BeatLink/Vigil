"""
Mosquitto MQTT broker health via a live publish/subscribe round trip.

Complements a `systemd_service` monitor on mosquitto rather than replacing it.
That one answers "is the process alive"; this one answers "is it still doing
its job", which is a different failure. The case that motivates it: the broker
process stays up and the port stays open while message routing itself is
wedged (a persistence-file write jam, a listener socket accepted but wired to
a dead internal queue) — every liveness and port check passes throughout,
while every client silently stops seeing new messages.

The only signal that catches that is an actual message delivered end to end:
this plugin subscribes to a private probe topic, publishes a unique payload to
it, and confirms that exact payload comes back within a timeout. Reading
`$SYS` broker stats was considered and rejected as the primary signal — `$SYS`
proves the broker is *reporting*, not that it is *routing*, and update
intervals for those topics are commonly 10s or slower, too coarse to catch a
routing stall promptly.

The round trip runs over SSH on the broker's own host with `mosquitto_sub`
backgrounded and `mosquitto_pub` following it, both authenticating as a
dedicated low-privilege MQTT user scoped (via that user's ACL) to the probe
topic only — this monitor can prove the broker delivers messages, and nothing
more, which matters because MQTT credentials are broker-wide unless an ACL
narrows them.

Config options:
  host              Broker address as seen from the monitored host (default:
                    127.0.0.1)
  port              Broker port (default: 1883)
  username          MQTT username for the probe round trip.
  password          MQTT password. Prefer password_command over inlining a
                    secret here — this value is readable in the config file.
  password_command  Command run on the monitored host whose stdout is the
                    password (e.g. "cat /run/secrets/mosquitto_vigil"). Takes
                    precedence over `password`.
  probe_topic       Topic the round trip publishes/subscribes on (default:
                    "vigil/probe/<id>" — namespaced by this monitor's id so
                    multiple Mosquitto monitors, or a real client, never
                    collide on the same topic).
  probe_timeout     Seconds to wait for the published message to come back
                    (default: 5)
"""
import shlex
import uuid
from typing import Any, Dict, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

# Emitted by the remote script on stderr when the round trip's own
# expectations are violated, distinguished from a bare SSH/command failure so
# the log can name which side of the exchange broke.
_TIMED_OUT = "VIGIL_MQTT_TIMEOUT"
_MISMATCH = "VIGIL_MQTT_MISMATCH"


def _auth_flags(username: Optional[str], password_command: Optional[str],
                password: Optional[str]) -> str:
    """Shell fragment adding -u/-P flags when credentials are configured."""
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
    """
    Build a shell script that publishes a nonce to `topic` and confirms it
    comes back on a subscription to the same topic, within `timeout` seconds.

    `mosquitto_sub -C 1` exits after receiving exactly one message, so the
    subscriber's own exit code (rather than a fixed sleep) is what bounds the
    round trip; `timeout` wraps it as a backstop against a broker that accepts
    the subscription but never delivers.
    """
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
    ['logs'],
]


class MosquittoPlugin(BasePlugin):
    """Monitors Mosquitto message delivery via a live publish/subscribe round trip."""

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

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('roundtrip_card'):
            roundtrip_label = info_card('ROUND TRIP', '--')
        with layout.cell('latency_card'):
            latency_label = info_card('LATENCY', '--')
        with layout.cell('chart'):
            history_chart('ROUND TRIP LATENCY (ms)', self.id, 'roundtrip_ms')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            ok = self.latest_metric('roundtrip_ok')
            latency = self.latest_metric('roundtrip_ms')

            if ok:
                passed = ok.value >= 1.0
                roundtrip_label.text = 'OK' if passed else 'FAILED'
                roundtrip_label.style(
                    f'color: {STATUS_COLORS["online" if passed else "failed"]}')
            if latency:
                latency_label.text = f'{latency.value:.0f} ms'

        safe_timer(5.0, update_cards)
