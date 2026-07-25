import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CmdResult, Command, CollectResult, HttpRequest, HttpResult, Request, Result,
)
from vigil.plugins.base.plugin_helpers import resolve_secret


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


class Trilium(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        # Vigil fetches this URL directly; it must be Vigil-reachable, no default.
        self.api_url = config.get('api_url')
        # Resolve the ETAPI token once, on the Vigil host, so requests() stays pure.
        self.token = resolve_secret(config.get('token'), config.get('token_command'))
        self.stale_warning = float(config.get('stale_warning', 72))
        self.api_timeout = int(config.get('api_timeout', 10))

        from vigil.core.ui.spec import register_color_rule
        self._color_rule_name = f'trilium_stale_{self.id}'

        @register_color_rule(self._color_rule_name)
        def _lastmod_color(v, _stale_warning=self.stale_warning):
            if v is None:
                return None
            return 'warning' if v >= _stale_warning else 'online'

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def requests(self) -> List[Request]:
        if not self.api_url:
            return []
        base = self.api_url.rstrip('/')
        return [HttpRequest(
            url=f"{base}/etapi/metrics?format=json", timeout=self.api_timeout,
            headers={"Authorization": self.token or ""},
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        if not results:
            return CollectResult.failed("No 'api_url' configured")

        result: HttpResult = results[0]
        if result.error is not None:
            return CollectResult.failed(f"Failed to query Trilium ETAPI: {result.error}")
        if result.status_code != 200:
            return CollectResult.failed(
                f"Trilium ETAPI returned HTTP {result.status_code} "
                f"(check the ETAPI token)")

        try:
            data = _parse_response(result.text)
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
        from vigil.core.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.spec import register_formatter


@register_formatter('trilium_age_ago')
def _lastmod_text(v):
    return '--' if v is None else f'{_format_age(v)} ago'
