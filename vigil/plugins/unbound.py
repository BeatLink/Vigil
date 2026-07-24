import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.web_plugin_base import UIPlugin

_SEP = "@@VIGIL_SPLIT@@"

_RESOLVE_OK_MARKERS = ("NOERROR",)


def _build_probe_script(control_cmd: str, query_host: str, query_port: int,
                        query_domain: str, query_timeout: int) -> str:
    lines = [
        "set -e",
        control_cmd,
        f'echo "{_SEP}"',
        (
            f'dig +time={int(query_timeout)} +tries=1 '
            f'@{shlex.quote(query_host)} -p {int(query_port)} '
            f'{shlex.quote(query_domain)} 2>&1 | grep -E "^;; ->>HEADER<<-" || true'
        ),
    ]
    return '\n'.join(lines)


def _parse_stats(raw: str) -> Dict[str, float]:
    stats: Dict[str, float] = {}
    for line in raw.splitlines():
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        try:
            stats[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return stats


def _resolved_ok(query_output: str) -> bool:
    return any(marker in query_output for marker in _RESOLVE_OK_MARKERS)


def _split_response(stdout: str) -> Tuple[str, str]:
    if _SEP not in stdout:
        raise ValueError(f"unexpected control output: {stdout[:200]!r}")
    stats_raw, query_raw = stdout.split(_SEP, 1)
    return stats_raw.strip(), query_raw.strip()


_DEFAULT_LAYOUT = [
    ['host_card', 'resolution_card', 'servfail_card'],
    ['queries_card', 'cache_card', 'uptime_card'],
    ['chart'],
    ['events'],
]


class UnboundCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.control_cmd = config.get('control_cmd', 'unbound-control stats_noreset')
        self.query_host = config.get('query_host', '127.0.0.1')
        self.query_port = int(config.get('query_port', 53))
        self.query_domain = config.get('query_domain', 'cloudflare.com')
        self.query_timeout = int(config.get('query_timeout', 5))
        self.servfail_warning = float(config.get('servfail_warning', 5))
        self.servfail_threshold = float(config.get('servfail_threshold', 20))
        self.min_queries = int(config.get('min_queries', 20))

    def commands(self) -> List[Command]:
        script = _build_probe_script(
            self.control_cmd, self.query_host, self.query_port,
            self.query_domain, self.query_timeout,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Unbound: {stderr.strip()}")

        try:
            stats_raw, query_output = _split_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        stats = _parse_stats(stats_raw)
        resolved = _resolved_ok(query_output)

        total_answered = stats.get('total.num.queries', 0.0)
        servfail = stats.get('total.num.servfail', 0.0)
        cache_hits = stats.get('total.num.cachehits', 0.0)
        cache_miss = stats.get('total.num.cachemiss', 0.0)
        uptime = stats.get('total.num.uptime', 0.0)

        servfail_rate = (100.0 * servfail / total_answered) if total_answered else 0.0
        cache_total = cache_hits + cache_miss
        cache_rate = (100.0 * cache_hits / cache_total) if cache_total else 0.0

        metrics = {
            'resolved_ok': 1.0 if resolved else 0.0,
            'queries_total': total_answered,
            'servfail_total': servfail,
            'servfail_rate_pct': servfail_rate,
            'cache_hit_rate_pct': cache_rate,
            'uptime_seconds': uptime,
        }

        problems = []
        level = 'online'

        def _escalate(new_level: str):
            nonlocal level
            order = ('online', 'warning', 'failed')
            if order.index(new_level) > order.index(level):
                level = new_level

        if not resolved:
            problems.append(f"probe lookup of {self.query_domain} did not resolve")
            _escalate('failed')

        if total_answered >= self.min_queries:
            if servfail_rate >= self.servfail_threshold:
                problems.append(
                    f"SERVFAIL rate {servfail_rate:.1f}% >= {self.servfail_threshold}%")
                _escalate('failed')
            elif servfail_rate >= self.servfail_warning:
                problems.append(
                    f"SERVFAIL rate {servfail_rate:.1f}% >= {self.servfail_warning}%")
                _escalate('warning')

        parts = [
            "resolved OK" if resolved else "resolution FAILED",
            f"{servfail_rate:.1f}% SERVFAIL",
            f"{int(total_answered):,} queries",
            f"{cache_rate:.1f}% cache hit",
        ]
        if problems:
            parts.append("| " + "; ".join(problems))

        log_level = "ERROR" if level == 'failed' else "WARNING" if level == 'warning' else "INFO"
        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), log_level)], status=level)


class UnboundUIPlugin(UIPlugin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.servfail_warning = float(self.config.get('servfail_warning', 5))
        self.servfail_threshold = float(self.config.get('servfail_threshold', 20))

        from vigil.web.ui.spec import register_color_rule, threshold_color
        self._color_rule_name = f'unbound_servfail_{self.id}'
        register_color_rule(self._color_rule_name)(
            threshold_color(warning=self.servfail_warning, threshold=self.servfail_threshold))

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'resolution_card': {
                    'metric': 'resolved_ok', 'title': 'RESOLUTION',
                    'format': 'unbound_resolution_text', 'color': 'unbound_resolution_color',
                },
                'servfail_card': {
                    'metric': 'servfail_rate_pct', 'title': 'SERVFAIL RATE',
                    'format': 'percent1_plain_dash', 'color': self._color_rule_name,
                },
                'queries_card': {'metric': 'queries_total', 'title': 'QUERIES', 'format': 'count_comma'},
                'cache_card': {
                    'metric': 'cache_hit_rate_pct', 'title': 'CACHE HIT RATE',
                    'format': 'percent1_plain_dash',
                },
                'uptime_card': {'metric': 'uptime_seconds', 'title': 'UPTIME', 'format': 'unbound_uptime'},
            },
            'chart': {'metric': 'servfail_rate_pct', 'title': 'SERVFAIL RATE (%)'},
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_formatter, register_color_rule


@register_formatter('unbound_resolution_text')
def _resolution_text(v):
    if v is None:
        return '--'
    return 'OK' if v >= 1.0 else 'FAILED'


@register_color_rule('unbound_resolution_color')
def _resolution_color(v):
    if v is None:
        return None
    return 'online' if v >= 1.0 else 'failed'


@register_formatter('unbound_uptime')
def _uptime_text(v):
    if v is None:
        return '--'
    days = int(v // 86400)
    hours = int((v % 86400) // 3600)
    return f'{days}d {hours}h' if days else f'{hours}h'
