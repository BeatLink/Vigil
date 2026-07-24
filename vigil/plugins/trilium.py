import json
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
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
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.api_url = config.get('api_url', 'http://127.0.0.1:8080')
        self.token = config.get('token')
        self.token_command = config.get('token_command')
        self.stale_warning = float(config.get('stale_warning', 72))
        self.api_timeout = int(config.get('api_timeout', 10))

    def commands(self) -> List[Command]:
        script = _build_fetch_script(
            self.api_url, self.api_timeout, self.token_command, self.token)
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Trilium ETAPI: {stderr.strip()}")

        try:
            data = _parse_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        stats = data.get('statistics', {})
        db = data.get('database', {})

        last_modified_age = _age_hours(stats.get('lastModified'))
        total_notes = float(db.get('totalNotes', 0) or 0)
        active_notes = float(db.get('activeNotes', 0) or 0)

        metrics = {'notes_total': total_notes, 'notes_active': active_notes}
        if last_modified_age is not None:
            metrics['last_modified_age_hours'] = last_modified_age

        if last_modified_age is None:
            return CollectResult(
                metrics=metrics,
                logs=[("No 'lastModified' timestamp in ETAPI response", "WARNING")],
                status='warning',
            )

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
        return CollectResult(metrics=metrics, logs=[(message, log_level)], status=level)


class TriliumUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale_warning = float(self.config.get('stale_warning', 72))

        from vigil.web.ui.spec import register_color_rule
        self._color_rule_name = f'trilium_stale_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _lastmod_color(v, _stale_warning=self.stale_warning):
            if v is None:
                return None
            return 'warning' if v >= _stale_warning else 'online'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'lastmod_card': {
                    'metric': 'last_modified_age_hours', 'title': 'LAST MODIFIED',
                    'format': 'trilium_age_ago', 'color': self._color_rule_name,
                },
                'notes_card': {'metric': 'notes_total', 'title': 'TOTAL NOTES', 'format': 'count_comma'},
            },
            'chart': {'metric': 'last_modified_age_hours', 'title': 'HOURS SINCE LAST MODIFIED'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter


@register_formatter('trilium_age_ago')
def _lastmod_text(v):
    return '--' if v is None else f'{_format_age(v)} ago'
