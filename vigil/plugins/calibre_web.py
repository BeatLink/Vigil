import shlex
from typing import Any, Dict, List, Optional

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.web_plugin_base import UIPlugin

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
        f'curl -s -m {timeout} {auth} -w "\\n{_SEP}%{{http_code}} %{{time_total}}" '
        f'{shlex.quote(base + "/opds")}'
    )
    return '\n'.join(lines)


def _parse_response(stdout: str) -> tuple:
    if _SEP not in stdout:
        raise ValueError(f"unexpected response: {stdout[:200]!r}")
    body, _, tail = stdout.rpartition(_SEP)
    parts = tail.strip().split()
    code = parts[0] if parts else ''
    time_total = parts[1] if len(parts) > 1 else '0'
    try:
        status = int(code.strip())
    except ValueError as e:
        raise ValueError(f"non-numeric status code {code.strip()!r}") from e
    try:
        elapsed_ms = float(time_total) * 1000.0
    except ValueError:
        elapsed_ms = 0.0
    return body.strip(), status, elapsed_ms


def _looks_like_opds(body: str) -> bool:
    head = body[:500]
    return '<feed' in head and ('atom' in head.lower() or 'opds' in head.lower())


_DEFAULT_LAYOUT = [
    ['host_card', 'feed_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class CalibreWebCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.url = config.get('url', 'http://127.0.0.1:8083')
        self.username = config.get('username', 'vigil')
        self.password = config.get('password')
        self.password_command = config.get('password_command')
        self.request_timeout = int(config.get('request_timeout', 10))

    def commands(self) -> List[Command]:
        script = _build_probe_script(
            self.url, self.request_timeout, self.username,
            self.password_command, self.password,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0:
            return CollectResult(
                metrics={'feed_ok': 0.0},
                logs=[(f"Failed to fetch OPDS feed: {stderr.strip()}", "ERROR")],
                status='failed',
            )

        try:
            body, status, elapsed_ms = _parse_response(stdout)
        except ValueError as e:
            return CollectResult(
                metrics={'feed_ok': 0.0},
                logs=[(str(e), "ERROR")],
                status='failed',
            )

        metrics = {'feed_status': float(status), 'feed_latency_ms': elapsed_ms}

        if status == 200 and _looks_like_opds(body):
            metrics['feed_ok'] = 1.0
            return CollectResult(
                metrics=metrics,
                logs=[(f"OPDS feed OK ({elapsed_ms:.0f}ms)", "INFO")],
                status='online',
            )

        metrics['feed_ok'] = 0.0
        if status == 401:
            message = ("OPDS request rejected (401) — check the vigil account "
                       "credentials")
        elif status == 200:
            message = ("OPDS request returned 200 but the body was not a valid "
                       "feed — likely a login or error page instead of real data")
        else:
            message = f"OPDS request returned unexpected status {status}"
        return CollectResult(metrics=metrics, logs=[(message, "ERROR")], status='failed')


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
