"""
BlockURL blocklist health via its own REST API.

Complements a `systemd_service` monitor on blockurl rather than replacing
it. That one answers "is the process alive"; this one answers "is the
blocklist database actually intact and non-empty", which is a different
failure — the process can bind its port and answer HTTP while the SQLite
file underneath has gone missing, corrupted, or been reset to empty, and a
liveness check alone would not notice.

BlockURL has no dedicated health/stats endpoint (it is a small personal
project, not designed with a monitor in mind) — `/urls/domains` is read
instead because a non-trivial domain count is itself evidence the database
is intact and populated, which is the only thing worth checking about it.

Authenticates with the same `X-API-Key` header the app itself supports for
non-interactive callers (see `before_request` in blockurl's __main__.py) —
the existing `blockurl_api_key` secret (blockurl.nix), no separate Vigil
credential needed. That secret file is in `KEY=value` form (it doubles as
the service's own EnvironmentFile), so the value after `=` is what gets sent.

Config options:
  api_url            Base URL of BlockURL, as seen from the monitored host
                     (default: http://127.0.0.1:9001)
  api_key            The API key value. Prefer api_key_command.
  api_key_command    Command run on the monitored host whose stdout is the
                     key, extracted from its KEY=value file (default:
                     "cut -d= -f2- /run/secrets/blockurl_api_key").
  min_domains        Domain count below which status is warning (default: 1
                     — an empty list means either a fresh install or a wiped
                     database; either way there's nothing to alert on until
                     the operator has actually populated it, so this is
                     deliberately lax rather than assuming a "normal" size).
  api_timeout        Seconds allowed for the remote curl call (default: 10)
"""
import json
import shlex
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


def _build_fetch_script(api_url: str, timeout: int, api_key_command: Optional[str],
                        api_key: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if api_key_command:
        lines.append(f"__key=$({api_key_command})")
        header = '-H "X-API-Key: $__key"'
    else:
        header = f'-H {shlex.quote("X-API-Key: " + (api_key or ""))}'

    lines.append(f'curl -s -m {timeout} {header} {shlex.quote(base + "/urls/domains")}')
    return '\n'.join(lines)


def _parse_response(stdout: str) -> list:
    """Parse the `[[domain, count], ...]` response, raising on anything else."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"domains response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(data, list):
        raise ValueError(f"domains response was not a list: {stdout[:200]!r}")
    return data


_DEFAULT_LAYOUT = [
    ['host_card', 'domains_card', 'urls_card'],
    ['chart'],
    ['events'],
]


class BlockurlCollectorPlugin(CollectorPlugin):
    """Monitors BlockURL's blocklist database health via its REST API."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:9001')
        self.api_key = config.get('api_key')
        self.api_key_command = config.get(
            'api_key_command', 'cut -d= -f2- /run/secrets/blockurl_api_key')
        self.min_domains = int(config.get('min_domains', 1))
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.api_key_command, self.api_key)
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query BlockURL API: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        domain_count = len(data)
        url_total = sum(int(entry[1]) for entry in data
                         if isinstance(entry, list) and len(entry) == 2)

        self.db_metrics.metric('domains_total', float(domain_count))
        self.db_metrics.metric('urls_total', float(url_total))

        if domain_count < self.min_domains:
            self.db_logger.write(
                f"Only {domain_count} domain(s) in the blocklist "
                f"(< {self.min_domains}) — database may be empty or wiped",
                level="WARNING")
            self.set_status('warning')
            return

        self.db_logger.write(
            f"{domain_count} domain(s), {url_total} blocked URL(s)", level="INFO")
        self.set_status('online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class BlockurlUIPlugin(UIPlugin):
    """Dashboard rendering for the blockurl monitor."""

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )
        page = self.page(metric_names=['domains_total', 'urls_total'])
        min_domains = int(self.config.get('min_domains', 1))

        def _urls_or_dash(v):
            return '--' if v is None else f'{int(v):,}'

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('domains_card'):
            domains_label = info_card('DOMAINS', '--').bind_text_from(
                page.model, ('metrics', 'domains_total'),
                backward=lambda v: '--' if v is None else str(int(v)))
        with layout.cell('urls_card'):
            info_card('BLOCKED URLS', '--').bind_text_from(
                page.model, ('metrics', 'urls_total'), backward=_urls_or_dash)
        with layout.cell('chart'):
            history_chart(page, 'BLOCKED URLS', self.id, 'urls_total')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            count = page.model.metrics.get('domains_total')
            if count is not None:
                domains_label.style(
                    f'color: {STATUS_COLORS["warning" if count < min_domains else "online"]}')

        page.on_refresh(update_color)
        page.start()
