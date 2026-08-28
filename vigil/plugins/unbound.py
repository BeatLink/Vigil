"""Unbound resolver health, from one script over SSH — sampled locally by the
agent on agent-backed hosts — combining unbound-control statistics with a
live dig probe against the resolver itself. Config: control_cmd, query_host,
query_port, query_domain, query_timeout, servfail_warning /
servfail_threshold (percent), min_queries. A failed probe lookup or a
SERVFAIL rate at servfail_threshold is failed; a rate at servfail_warning is
warning, with rates only judged once min_queries have been answered."""

import shlex
from typing import Any, Dict, List, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.plugins.base.plugin_helpers import SCRIPT_SEP, StatusAccumulator
from vigil.core.connectors.types import CmdResult, Command, CollectResult

_RESOLVE_OK_MARKERS = ("NOERROR",)


def _build_probe_script(control_cmd: str, query_host: str, query_port: int,
                        query_domain: str, query_timeout: int) -> str:
    lines = [
        "set -e",
        control_cmd,
        f'echo "{SCRIPT_SEP}"',
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
    if SCRIPT_SEP not in stdout:
        raise ValueError(f"unexpected control output: {stdout[:200]!r}")
    stats_raw, query_raw = stdout.split(SCRIPT_SEP, 1)
    return stats_raw.strip(), query_raw.strip()


def _stats_metrics(stats: Dict[str, float], resolved: bool) -> Dict[str, float]:
    """Derive the metric dict (totals, SERVFAIL and cache-hit rates, uptime) from the control stats and probe outcome."""
    total_answered = stats.get('total.num.queries', 0.0)
    servfail = stats.get('total.num.servfail', 0.0)
    cache_hits = stats.get('total.num.cachehits', 0.0)
    cache_miss = stats.get('total.num.cachemiss', 0.0)
    cache_total = cache_hits + cache_miss
    return {
        'resolved_ok': 1.0 if resolved else 0.0,
        'queries_total': total_answered,
        'servfail_total': servfail,
        'servfail_rate_pct': (100.0 * servfail / total_answered) if total_answered else 0.0,
        'cache_hit_rate_pct': (100.0 * cache_hits / cache_total) if cache_total else 0.0,
        'uptime_seconds': stats.get('total.num.uptime', 0.0),
    }


def _accumulate_resolver_problems(resolved: bool, total_answered: float,
                                  servfail_rate: float, query_domain: str,
                                  servfail_warning: float, servfail_threshold: float,
                                  min_queries: int) -> StatusAccumulator:
    """Judge the probe outcome and SERVFAIL rate against the thresholds."""
    acc = StatusAccumulator()
    if not resolved:
        acc.escalate('failed', f"probe lookup of {query_domain} did not resolve")
    if total_answered >= min_queries:
        if servfail_rate >= servfail_threshold:
            acc.escalate('failed',
                         f"SERVFAIL rate {servfail_rate:.1f}% >= {servfail_threshold}%")
        elif servfail_rate >= servfail_warning:
            acc.escalate('warning',
                         f"SERVFAIL rate {servfail_rate:.1f}% >= {servfail_warning}%")
    return acc


_DEFAULT_LAYOUT = [
    ['host_card', 'resolution_card', 'servfail_card'],
    ['queries_card', 'cache_card', 'uptime_card'],
    ['chart'],
    ['events'],
]


class Unbound(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.control_cmd = config.get('control_cmd', 'unbound-control stats_noreset')
        self.query_host = config.get('query_host', '127.0.0.1')
        self.query_port = int(config.get('query_port', 53))
        self.query_domain = config.get('query_domain', 'cloudflare.com')
        self.query_timeout = int(config.get('query_timeout', 5))
        self.servfail_warning = float(config.get('servfail_warning', 5))
        self.servfail_threshold = float(config.get('servfail_threshold', 20))
        self.min_queries = int(config.get('min_queries', 20))

        from vigil.core.ui.spec import threshold_color
        self._servfail_color = threshold_color(
            warning=self.servfail_warning, threshold=self.servfail_threshold)

    SAMPLED = True

    def commands(self) -> List[Command]:
        script = _build_probe_script(
            self.control_cmd, self.query_host, self.query_port,
            self.query_domain, self.query_timeout,
        )
        return [Command(script)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        """Turns the stats+dig-probe script output into a CollectResult with
        query/SERVFAIL/cache/uptime metrics, one summary log line, and a status
        where a failed probe lookup or a SERVFAIL rate at the threshold is failed
        and a rate at the warning level is warning."""
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to query Unbound: {stderr.strip()}")

        try:
            stats_raw, query_output = _split_response(stdout)
        except ValueError as e:
            return CollectResult.failed(str(e))

        stats = _parse_stats(stats_raw)
        resolved = _resolved_ok(query_output)
        metrics = _stats_metrics(stats, resolved)

        acc = _accumulate_resolver_problems(
            resolved, metrics['queries_total'], metrics['servfail_rate_pct'],
            self.query_domain, self.servfail_warning, self.servfail_threshold,
            self.min_queries)

        parts = [
            "resolved OK" if resolved else "resolution FAILED",
            f"{metrics['servfail_rate_pct']:.1f}% SERVFAIL",
            f"{int(metrics['queries_total']):,} queries",
            f"{metrics['cache_hit_rate_pct']:.1f}% cache hit",
        ]
        if acc.problems:
            parts.append("| " + "; ".join(acc.problems))

        return CollectResult(metrics=metrics, logs=[(' | '.join(parts), acc.log_level)], status=acc.status)

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
                    'format': 'percent1_plain_dash', 'color': self._servfail_color,
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


from vigil.core.ui.spec import register_formatter, register_color_rule


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
