"""
Radicale CalDAV/CardDAV health via a live WebDAV PROPFIND.

Complements a `systemd_service` monitor on radicale rather than replacing it.
That one answers "is the process alive"; this one answers "is it still
speaking CalDAV/CardDAV correctly", which is a different failure. Radicale has
no JSON status API at all — it is pure WebDAV — so the only real signal
available is issuing the same kind of request a calendar client would and
checking the response is what WebDAV actually expects, not just any HTTP
response. The case this catches: the process is up and the port answers, but
the htpasswd file is corrupt, storage permissions are wrong, or the
collections directory is missing — every liveness check stays green while
every client gets rejected or served empty/broken data.

A PROPFIND with Depth: 0 against the collection root is the standard
lightweight WebDAV health probe: a healthy server answers `207 Multi-Status`
with an XML body; a server that is up but broken in some other way typically
answers `401` (bad credentials/htpasswd), `500` (an internal fault), or
closes the connection outright.

Runs over SSH on Radicale's own host, authenticating as a dedicated `vigil`
account added to the same htpasswd file (see radicale.nix) — kept separate
from the real login so a credential leak here cannot expose anyone's actual
calendar or contacts, only prove the collection root answers PROPFIND
correctly.

Config options:
  url             Base URL of the Radicale server, as seen from the
                  monitored host (default: http://127.0.0.1:5232)
  username        Basic auth username for the probe (default: "vigil")
  password        Basic auth password. Prefer password_command over inlining
                  a secret here.
  password_command
                  Command run on the monitored host whose stdout is the
                  password (e.g. "cat /run/secrets/radicale_vigil_password").
                  Takes precedence over `password`.
  request_timeout Seconds allowed for the PROPFIND request (default: 10)
"""
import shlex
from typing import Any, Dict, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, safe_timer
from vigil.core.ui.theme import STATUS_COLORS

_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>'
)

# Marks the end of the HTTP status code so the response body can be
# inspected alongside it in one round trip.
_SEP = "@@VIGIL_SPLIT@@"


def _build_probe_script(url: str, timeout: int, username: Optional[str],
                        password_command: Optional[str], password: Optional[str]) -> str:
    """
    Build a shell script that issues one Depth: 0 PROPFIND against the
    collection root and reports the HTTP status code alongside the body.
    """
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
    """Split the body from the trailing HTTP status code."""
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
    ['logs'],
]


class RadicalePlugin(BasePlugin):
    """Monitors Radicale CalDAV/CardDAV health via a live PROPFIND request."""

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

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('propfind_card'):
            propfind_label = info_card('PROPFIND', '--')
        with layout.cell('latency_card'):
            latency_label = info_card('LATENCY', '--')
        with layout.cell('chart'):
            history_chart('PROPFIND LATENCY (ms)', self.id, 'propfind_latency_ms')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        def update_cards():
            ok = self.latest_metric('propfind_ok')
            latency = self.latest_metric('propfind_latency_ms')

            if ok:
                passed = ok.value >= 1.0
                propfind_label.text = 'OK' if passed else 'FAILED'
                propfind_label.style(
                    f'color: {STATUS_COLORS["online" if passed else "failed"]}')
            if latency:
                latency_label.text = f'{latency.value:.0f} ms'

        safe_timer(5.0, update_cards)
