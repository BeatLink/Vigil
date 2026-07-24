import shlex
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>'
)

_SEP = "@@VIGIL_SPLIT@@"


def _build_probe_script(url: str, timeout: int, username: Optional[str],
                        password_command: Optional[str], password: Optional[str]) -> str:
    base = url.rstrip('/')
    lines = ["set -e"]

    auth = ''
    if username:
        if password_command:
            lines.append(f"__pw=$({password_command})")
            auth = f'-u {shlex.quote(username)}:"$__pw"'
        elif password:
            auth = f'-u {shlex.quote(username + ":" + password)}'
        else:
            auth = f'-u {shlex.quote(username)}'

    lines.append(
        f'curl -s -m {timeout} -X PROPFIND {auth} '
        f'-H "Depth: 0" -H "Content-Type: application/xml" '
        f'--data {shlex.quote(_PROPFIND_BODY)} '
        f'-w "\\n{_SEP}%{{http_code}}" '
        f'{shlex.quote(base + "/")}'
    )
    return '\n'.join(lines)


def _parse_response(stdout: str) -> tuple:
    if _SEP not in stdout:
        raise ValueError(f"unexpected response: {stdout[:200]!r}")
    body, _, code = stdout.rpartition(_SEP)
    try:
        status = int(code.strip())
    except ValueError as e:
        raise ValueError(f"non-numeric status code {code.strip()!r}") from e
    return body.strip(), status


_DEFAULT_LAYOUT = [
    ['host_card', 'propfind_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class RadicaleCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.url = config.get('url', 'http://127.0.0.1:5232')
        self.username = config.get('username', 'vigil')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.request_timeout = int(config.get('request_timeout', 10))

    async def on_collect(self):
        script = _build_probe_script(
            self.url, self.request_timeout, self.username,
            self.password_command, self.password,
        )
        import time as _time
        started = _time.monotonic()
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        elapsed_ms = (_time.monotonic() - started) * 1000.0

        if ret != 0:
            self.db_logger.write(f"Failed to run PROPFIND probe: {stderr.strip()}", level="ERROR")
            self.db_metrics.metric('propfind_ok', 0.0)
            self.set_status('failed')
            return

        try:
            body, status = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.db_metrics.metric('propfind_ok', 0.0)
            self.set_status('failed')
            return

        self.db_metrics.metric('propfind_status', float(status))
        self.db_metrics.metric('propfind_latency_ms', elapsed_ms)

        if status == 207:
            self.db_metrics.metric('propfind_ok', 1.0)
            self.db_logger.write(
                f"PROPFIND OK (207 Multi-Status, {elapsed_ms:.0f}ms)", level="INFO")
            self.set_status('online')
            return

        self.db_metrics.metric('propfind_ok', 0.0)
        if status == 401:
            self.db_logger.write(
                "PROPFIND rejected (401) — check the vigil htpasswd entry "
                "is present and matches password_command", level="ERROR")
        elif status == 0:
            self.db_logger.write(
                "PROPFIND got no response (connection failed)", level="ERROR")
        else:
            self.db_logger.write(
                f"PROPFIND returned unexpected status {status}: {body[:200]}",
                level="ERROR")
        self.set_status('failed')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class RadicaleUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'propfind_card': {
                'metric': 'propfind_ok', 'title': 'PROPFIND',
                'format': 'radicale_ok_text', 'color': 'radicale_ok_color',
            },
            'latency_card': {'metric': 'propfind_latency_ms', 'title': 'LATENCY', 'format': 'ms0'},
        },
        'chart': {'metric': 'propfind_latency_ms', 'title': 'PROPFIND LATENCY (ms)'},
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter, register_color_rule


@register_formatter('radicale_ok_text')
def _propfind_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('radicale_ok_color')
def _propfind_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
