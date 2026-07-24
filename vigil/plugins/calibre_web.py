import shlex
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

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
        f'curl -s -m {timeout} {auth} -w "\\n{_SEP}%{{http_code}}" '
        f'{shlex.quote(base + "/opds")}'
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


def _looks_like_opds(body: str) -> bool:
    head = body[:500]
    return '<feed' in head and ('atom' in head.lower() or 'opds' in head.lower())


_DEFAULT_LAYOUT = [
    ['host_card', 'feed_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class CalibreWebCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.url = config.get('url', 'http://127.0.0.1:8083')
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
            self.db_logger.write(f"Failed to fetch OPDS feed: {stderr.strip()}", level="ERROR")
            self.db_metrics.metric('feed_ok', 0.0)
            self.set_status('failed')
            return

        try:
            body, status = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.db_metrics.metric('feed_ok', 0.0)
            self.set_status('failed')
            return

        self.db_metrics.metric('feed_status', float(status))
        self.db_metrics.metric('feed_latency_ms', elapsed_ms)

        if status == 200 and _looks_like_opds(body):
            self.db_metrics.metric('feed_ok', 1.0)
            self.db_logger.write(
                f"OPDS feed OK ({elapsed_ms:.0f}ms)", level="INFO")
            self.set_status('online')
            return

        self.db_metrics.metric('feed_ok', 0.0)
        if status == 401:
            self.db_logger.write(
                "OPDS request rejected (401) — check the vigil account "
                "credentials", level="ERROR")
        elif status == 200:
            self.db_logger.write(
                "OPDS request returned 200 but the body was not a valid "
                "feed — likely a login or error page instead of real data",
                level="ERROR")
        else:
            self.db_logger.write(
                f"OPDS request returned unexpected status {status}", level="ERROR")
        self.set_status('failed')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class CalibreWebUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'feed_card': {
                'metric': 'feed_ok', 'title': 'OPDS FEED',
                'format': 'calibre_web_ok_text', 'color': 'calibre_web_ok_color',
            },
            'latency_card': {'metric': 'feed_latency_ms', 'title': 'LATENCY', 'format': 'ms0'},
        },
        'chart': {'metric': 'feed_latency_ms', 'title': 'OPDS LATENCY (ms)'},
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter, register_color_rule


@register_formatter('calibre_web_ok_text')
def _feed_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('calibre_web_ok_color')
def _feed_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'
