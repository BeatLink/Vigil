"""
Calibre-Web library health via a live OPDS feed request.

Complements a `systemd_service` monitor on calibre-web-automated rather than
replacing it. That one answers "is the process alive"; this one answers "is
the metadata database actually being served", which is a different failure.
Calibre-Web has no dedicated health/status API in either upstream
(janeczku/calibre-web) or the Automated fork — the project's own Docker
`HEALTHCHECK` is just `curl -f http://localhost:8083/`, and a known upstream
issue (crocodilestick/Calibre-Web-Automated#1134) documents that even that
can return 200 while the metadata DB is actually broken. `/opds`, in
contrast, genuinely exercises the DB layer to build its feed, so a request
that returns valid OPDS XML is stronger evidence than root responding 200.

Requests `/opds` with the credentials of a dedicated low-privilege account
(download-only, no admin rights) created once by hand — see
calibre-web-automated.nix for why that step cannot be made declarative — and
checks both the HTTP status and that the body is actually an Atom/OPDS feed,
since a login or error page can still answer 200.

Config options:
  url               Base URL of Calibre-Web, as seen from the monitored host
                    (default: http://127.0.0.1:8083)
  username          Basic auth username for the probe account.
  password          Basic auth password. Prefer password_command.
  password_command  Command run on the monitored host whose stdout is the
                    password (e.g. "cat /run/secrets/
                    calibre_web_vigil_password"). Takes precedence over
                    `password`.
  request_timeout   Seconds allowed for the request (default: 10)
"""
import shlex
from typing import Any, Dict, Optional

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, on_data_event
from vigil.core.ui.theme import STATUS_COLORS

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
    """True if the body is a real Atom/OPDS feed rather than an HTML page."""
    head = body[:500]
    return '<feed' in head and ('atom' in head.lower() or 'opds' in head.lower())


_DEFAULT_LAYOUT = [
    ['host_card', 'feed_card', 'latency_card'],
    ['chart'],
    ['events'],
]


class CalibreWebPlugin(BasePlugin):
    """Monitors Calibre-Web library health via a live OPDS feed request."""

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

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('feed_card'):
            feed_label = info_card('OPDS FEED', '--')
        with layout.cell('latency_card'):
            latency_label = info_card('LATENCY', '--')
        with layout.cell('chart'):
            history_chart('OPDS LATENCY (ms)', self.id, 'feed_latency_ms')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            ok = self.latest_metric('feed_ok')
            latency = self.latest_metric('feed_latency_ms')

            if ok:
                passed = ok.value >= 1.0
                feed_label.text = 'OK' if passed else 'FAILED'
                feed_label.style(
                    f'color: {STATUS_COLORS["online" if passed else "failed"]}')
            if latency:
                latency_label.text = f'{latency.value:.0f} ms'

        on_data_event('metric', feed_label, update_cards)
