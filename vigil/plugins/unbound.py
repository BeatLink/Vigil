"""
Unbound recursive resolver health via `unbound-control` and a live query.

Complements a `systemd_service` monitor on unbound rather than replacing it.
That one answers "is the process alive"; this one answers "is it still doing
its job", which is a different failure. The case that motivates it: the daemon
keeps running, the socket accepts connections, but recursion is broken (root
hints expired, the network path outbound is gone, DNSSEC validation is wedged)
and every query started failing SERVFAIL. A liveness check sees a healthy
process throughout.

Two signals carry that:

  live resolution   An actual query issued against the resolver for a domain
                    that should always answer. Proves the whole path — socket,
                    recursion, upstream reachability — works right now, which
                    stats alone cannot: a resolver can report a perfect cache
                    hit rate while every *new* lookup fails.
  SERVFAIL rate     Share of answered queries that came back SERVFAIL, read
                    from `unbound-control stats_noreset`. Catches partial
                    failure (a flaky upstream, a validation problem affecting
                    some but not all zones) that a single-domain probe can
                    miss because that one domain happens to still resolve.

Both run over SSH on the resolver's own host: `stats_noreset` reads Unbound's
control socket, which — per `localControlSocketPath` — is only reachable by
members of Unbound's own group, and the probe query targets the resolver's
loopback/LAN listener, which is not exposed beyond that host's `access-control`
either. Neither is reachable from Vigil's own process.

`stats_noreset` (rather than `stats`, which also resets) is deliberate: it
leaves Unbound's own counters alone, so Vigil's poll interval never fights
with any other consumer of the same stats (a shell alias, a separate exporter)
over who gets to zero them.

Config options:
  control_cmd       Command run on the monitored host to read stats (default:
                    "unbound-control stats_noreset"). Override if
                    unbound-control needs an explicit -c config path.
  query_host        Resolver address the probe query is sent to (default:
                    127.0.0.1)
  query_port        Resolver port (default: 53)
  query_domain      Domain looked up to prove live resolution (default:
                    "cloudflare.com" — stable, always-delegated, and outside
                    this network so a false pass requires real upstream
                    reachability)
  query_timeout     Seconds allowed for the probe query (default: 5)
  servfail_warning  SERVFAIL % at or above which status is warning (default: 5)
  servfail_threshold
                    SERVFAIL % at or above which status is failed (default: 20)
  min_queries       Total answered queries needed before the SERVFAIL rate is
                    judged at all (default: 20). Below this the ratio is noise
                    — a couple of queries since a restart legitimately swing
                    from 0% to 100% on a single failure.
"""
import re
import shlex
from typing import Any, Dict, Optional, Tuple

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart, on_data_event
from vigil.core.ui.theme import STATUS_COLORS

# Marks the end of the stats payload so the probe query's own output can
# follow in the same SSH round trip and be split apart again.
_SEP = "@@VIGIL_SPLIT@@"

# Text `drill`/`dig` emit on the status line of an answer that actually
# resolved, as opposed to timing out or being refused outright.
_RESOLVE_OK_MARKERS = ("NOERROR",)


def _build_probe_script(control_cmd: str, query_host: str, query_port: int,
                        query_domain: str, query_timeout: int) -> str:
    """
    Build a shell script that reads stats and issues one live query with
    `dig`, printing the response's header line in a form the parser can find.
    """
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
    """
    Parse `unbound-control stats_noreset` output into a flat dict.

    Each line is `name=value`; non-numeric values (there are none in stock
    output, but a custom build might add some) are skipped rather than
    raising, so one odd line cannot take the whole monitor down.
    """
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
    """
    True if the probe query's output shows a successful (NOERROR) answer.

    Anything else — SERVFAIL, REFUSED, a timeout with no header line at all —
    is treated as a failed resolution.
    """
    return any(marker in query_output for marker in _RESOLVE_OK_MARKERS)


def _split_response(stdout: str) -> Tuple[str, str]:
    """Split the combined stdout into (stats_raw, query_output)."""
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


class UnboundPlugin(BasePlugin):
    """Monitors Unbound recursive resolution health via unbound-control and a live query."""

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.control_cmd = config.get('control_cmd', 'unbound-control stats_noreset')
        self.query_host = config.get('query_host', '127.0.0.1')
        self.query_port = int(config.get('query_port', 53))
        self.query_domain = config.get('query_domain', 'cloudflare.com')
        self.query_timeout = int(config.get('query_timeout', 5))
        self.servfail_warning = float(config.get('servfail_warning', 5))
        self.servfail_threshold = float(config.get('servfail_threshold', 20))
        self.min_queries = int(config.get('min_queries', 20))

    async def on_collect(self):
        script = _build_probe_script(
            self.control_cmd, self.query_host, self.query_port,
            self.query_domain, self.query_timeout,
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(script)
        if ret != 0:
            self.db_logger.write(f"Failed to query Unbound: {stderr.strip()}", level="ERROR")
            self.set_status('failed')
            return

        try:
            stats_raw, query_output = _split_response(stdout)
        except ValueError as e:
            self.db_logger.write(str(e), level="ERROR")
            self.set_status('failed')
            return

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

        self.db_metrics.metric('resolved_ok', 1.0 if resolved else 0.0)
        self.db_metrics.metric('queries_total', total_answered)
        self.db_metrics.metric('servfail_total', servfail)
        self.db_metrics.metric('servfail_rate_pct', servfail_rate)
        self.db_metrics.metric('cache_hit_rate_pct', cache_rate)
        self.db_metrics.metric('uptime_seconds', uptime)

        # --- status ---------------------------------------------------------
        # Each condition is judged independently and the worst one wins, so a
        # healthy SERVFAIL rate cannot mask a probe that just failed to
        # resolve at all (or vice versa).
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

        # Below min_queries the ratio is dominated by whatever handful of
        # lookups happened to arrive, so it is not evidence of anything.
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
        self.db_logger.write(' | '.join(parts), level=log_level)
        self.set_status(level)

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
        with layout.cell('resolution_card'):
            resolution_label = info_card('RESOLUTION', '--')
        with layout.cell('servfail_card'):
            servfail_label = info_card('SERVFAIL RATE', '--')
        with layout.cell('queries_card'):
            queries_label = info_card('QUERIES', '--')
        with layout.cell('cache_card'):
            cache_label = info_card('CACHE HIT RATE', '--')
        with layout.cell('uptime_card'):
            uptime_label = info_card('UPTIME', '--')
        with layout.cell('chart'):
            history_chart('SERVFAIL RATE (%)', self.id, 'servfail_rate_pct')
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update_cards():
            resolved  = self.latest_metric('resolved_ok')
            servfail  = self.latest_metric('servfail_rate_pct')
            total     = self.latest_metric('queries_total')
            cache     = self.latest_metric('cache_hit_rate_pct')
            uptime    = self.latest_metric('uptime_seconds')

            if resolved:
                ok = resolved.value >= 1.0
                resolution_label.text = 'OK' if ok else 'FAILED'
                resolution_label.style(f'color: {STATUS_COLORS["online" if ok else "failed"]}')
            if servfail:
                servfail_label.text = f'{servfail.value:.1f}%'
                if servfail.value >= self.servfail_threshold:
                    colour = STATUS_COLORS['failed']
                elif servfail.value >= self.servfail_warning:
                    colour = STATUS_COLORS['warning']
                else:
                    colour = STATUS_COLORS['online']
                servfail_label.style(f'color: {colour}')
            if total:
                queries_label.text = f'{int(total.value):,}'
            if cache:
                cache_label.text = f'{cache.value:.1f}%'
            if uptime:
                days = int(uptime.value // 86400)
                hours = int((uptime.value % 86400) // 3600)
                uptime_label.text = f'{days}d {hours}h' if days else f'{hours}h'

        on_data_event('metric', resolution_label, update_cards)
