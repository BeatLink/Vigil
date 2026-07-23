"""
Trilium Notes write-activity health via the ETAPI metrics endpoint.

Complements a `systemd_service` monitor on trilium-server rather than
replacing it. That one answers "is the process alive"; this one answers "is
the note database actually still being written to", which is a different
failure — Trilium can serve its login page and API perfectly while, say, the
underlying SQLite file has gone read-only or a sync partner stopped applying
changes, and every liveness check stays green throughout.

Trilium's ETAPI exposes no explicit health/sync-status field (there is no
public, documented endpoint for that — the wiki-mentioned internal sync-check
route is session-cookie-authenticated for the web client only, not part of
ETAPI, and not worth depending on). The best available proxy is
`statistics.lastModified` from `/etapi/metrics?format=json`: if notes are
being actively used, this timestamp keeps advancing; if it goes stale for
longer than expected, either nothing is being written (which may be entirely
normal overnight) or something is actually broken. Because "normal" here
depends heavily on how the notes are used, this defaults to a generous
staleness window and is meant to be tuned per-instance rather than trusted
out of the box.

Authenticates with an ETAPI token generated once by hand — see trilium.nix
for why that step cannot be made declarative.

Config options:
  api_url            Base URL of the Trilium instance, as seen from the
                     monitored host (default: http://127.0.0.1:8080)
  token              ETAPI token. Prefer token_command.
  token_command      Command run on the monitored host whose stdout is the
                     token (e.g. "cat /run/secrets/trilium_etapi_token").
                     Takes precedence over `token`.
  stale_warning      Hours since the last note modification at which status
                     is warning (default: 72 — three days)
  api_timeout        Seconds allowed for the remote curl call (default: 10)
"""
import json
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin


def _build_fetch_script(api_url: str, timeout: int, token_command: Optional[str],
                        token: Optional[str]) -> str:
    base = api_url.rstrip('/')
    lines = ["set -e"]

    if token_command:
        lines.append(f"__token=$({token_command})")
        auth = '-H "Authorization: $__token"'
    else:
        auth = f'-H {shlex.quote("Authorization: " + (token or ""))}'

    lines.append(
        f'curl -s -m {timeout} {auth} '
        f'{shlex.quote(base + "/etapi/metrics?format=json")}'
    )
    return '\n'.join(lines)


def _parse_response(stdout: str) -> Dict[str, Any]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"metrics response was not JSON ({e}): {stdout[:200]!r}") from e
    if not isinstance(data, dict) or 'statistics' not in data:
        raise ValueError(f"response missing 'statistics' (check the ETAPI token): {stdout[:200]!r}")
    return data


def _age_hours(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def _format_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


_DEFAULT_LAYOUT = [
    ['host_card', 'lastmod_card', 'notes_card'],
    ['chart'],
    ['events'],
]


class TriliumCollectorPlugin(CollectorPlugin):
    """Monitors Trilium note write-activity via the ETAPI metrics endpoint."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8080')
        self.token = config.get('token')
        self.token_command = config.get('token_command')
        self.stale_warning = float(config.get('stale_warning', 72))
        self.api_timeout = int(config.get('api_timeout', 10))

    async def on_collect(self):
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.token_command, self.token)
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Trilium ETAPI: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

        stats = data.get('statistics', {})
        db = data.get('database', {})

        last_modified_age = _age_hours(stats.get('lastModified'))
        total_notes = float(db.get('totalNotes', 0) or 0)
        active_notes = float(db.get('activeNotes', 0) or 0)

        self.db_metrics.metric('notes_total', total_notes)
        self.db_metrics.metric('notes_active', active_notes)
        if last_modified_age is not None:
            self.db_metrics.metric('last_modified_age_hours', last_modified_age)

        # --- status ---------------------------------------------------------
        if last_modified_age is None:
            self.db_logger.write(
                "No 'lastModified' timestamp in ETAPI response", level="WARNING")
            self.set_status('warning')
            return

        if last_modified_age >= self.stale_warning:
            level = 'warning'
            message = (
                f"no note modified in {_format_age(last_modified_age)} "
                f"(>= {_format_age(self.stale_warning)} threshold) | "
                f"{int(total_notes):,} total notes"
            )
        else:
            level = 'online'
            message = (
                f"last modified {_format_age(last_modified_age)} ago | "
                f"{int(total_notes):,} total notes"
            )

        log_level = "WARNING" if level == 'warning' else "INFO"
        self.db_logger.write(message, level=log_level)
        self.set_status(level)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class TriliumUIPlugin(UIPlugin):
    """Dashboard rendering for the trilium monitor."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale_warning = float(self.config.get('stale_warning', 72))

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card, history_chart, on_data_event
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(
            self.config,
            _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT),
        )

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('lastmod_card'):
            lastmod_label = info_card('LAST MODIFIED', '--')
        with layout.cell('notes_card'):
            notes_label = info_card('TOTAL NOTES', '--')
        with layout.cell('chart'):
            history_chart('HOURS SINCE LAST MODIFIED', self.id, 'last_modified_age_hours')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            age   = self.latest_metric('last_modified_age_hours')
            total = self.latest_metric('notes_total')

            if age:
                lastmod_label.text = f'{_format_age(age.value)} ago'
                lastmod_label.style(
                    f'color: {STATUS_COLORS["warning" if age.value >= self.stale_warning else "online"]}')
            if total:
                notes_label.text = f'{int(total.value):,}'

        on_data_event('metric', lastmod_label, update_cards)
