"""
DNS record monitoring.

Checks that a DNS record resolves, and optionally that its answer matches an
expected value — catching a stale record, a botched DNS migration, or a
provider outage, independent of whatever host actually serves the domain.

Unlike the SSH-based plugins, there is no target host to reach: the query is
issued from wherever Vigil itself runs (or from an explicit `resolver`), so
this runs in-process like vigil_self rather than via SSHCollector. That also
means the resolver's own health is in view — pointing `resolver` at an
internal DNS server doubles as a liveness probe for it, distinct from
unbound.py's SERVFAIL-rate monitoring of one specific resolver's stats.

Config options:
  domain      Domain name to query                                (required)
  record_type One of A, AAAA, CNAME, MX, TXT, NS, SOA (default: "A")
  resolver    Resolver IP to query directly (default: system resolver)
  port        Resolver port (default: 53)
  timeout     Query timeout in seconds (default: 5)
  expected    Optional list of acceptable answer values. Any answer not in
              this list fails the monitor — for pinning an A record to known
              IPs, or an MX record to a known mail provider. Order-independent;
              only presence in the answer set is checked, since authoritative
              order is not meaningful for most record types.
"""
import asyncio
from typing import Any, Dict, List, Optional

import dns.exception
import dns.resolver

from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, on_data_event
from vigil.core.ui.theme import STATUS_COLORS

_DEFAULT_LAYOUT = [
    ['status_card', 'type_card', 'ttl_card'],
    ['answer'],
    ['events'],
]


def _answer_to_str(record_type: str, rdata) -> str:
    """Render one answer record as the plain string `expected` is compared against."""
    if record_type == 'MX':
        return f"{rdata.preference} {str(rdata.exchange).rstrip('.')}".strip()
    if record_type == 'TXT':
        return ''.join(part.decode('utf-8', 'replace') if isinstance(part, bytes) else part
                       for part in rdata.strings)
    return str(rdata).rstrip('.')


class DnsRecordPlugin(BasePlugin):
    """
    Monitors a DNS record: resolves it on each cycle and reports failed if the
    query errors, times out, or (when `expected` is set) returns none of the
    accepted values.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.domain = config.get('domain')
        self.record_type = str(config.get('record_type', 'A')).upper()
        self.resolver_addr = config.get('resolver')
        self.port = int(config.get('port', 53))
        self.timeout = float(config.get('timeout', 5))
        expected = config.get('expected')
        self.expected: Optional[List[str]] = (
            [str(v).rstrip('.') for v in expected] if expected else None
        )
        # DNS queries have nothing to do with SSH; this monitor is about the
        # domain, not a host in the fleet.
        self.target = self.domain or self.name

    def _make_resolver(self) -> "dns.resolver.Resolver":
        resolver = dns.resolver.Resolver(configure=self.resolver_addr is None)
        if self.resolver_addr:
            resolver.nameservers = [self.resolver_addr]
        resolver.port = self.port
        resolver.timeout = self.timeout
        resolver.lifetime = self.timeout
        return resolver

    async def on_collect(self):
        if not self.domain:
            self.db_logger.write("No 'domain' configured", level="ERROR")
            self.set_status('failed')
            return

        try:
            answers = await asyncio.to_thread(self._query)
        except dns.resolver.NXDOMAIN:
            self.db_logger.write(f"{self.domain} does not exist (NXDOMAIN)", level="ERROR")
            self.db_metrics.metric('resolved', 0.0)
            self.set_status('failed')
            return
        except dns.resolver.NoAnswer:
            self.db_logger.write(
                f"{self.domain} has no {self.record_type} record", level="ERROR"
            )
            self.db_metrics.metric('resolved', 0.0)
            self.set_status('failed')
            return
        except dns.exception.Timeout:
            self.db_logger.write(
                f"Query for {self.domain} ({self.record_type}) timed out after {self.timeout}s",
                level="ERROR"
            )
            self.db_metrics.metric('resolved', 0.0)
            self.set_status('failed')
            return
        except dns.exception.DNSException as e:
            self.db_logger.write(f"DNS query failed: {e}", level="ERROR")
            self.db_metrics.metric('resolved', 0.0)
            self.set_status('failed')
            return

        values = [_answer_to_str(self.record_type, r) for r in answers]
        ttl = answers.rrset.ttl if answers.rrset is not None else None

        self.db_metrics.metric('resolved', 1.0)
        if ttl is not None:
            self.db_metrics.metric('ttl', float(ttl))
        self._store_values(values)

        if self.expected is not None:
            unexpected = [v for v in values if v not in self.expected]
            if unexpected:
                self.db_logger.write(
                    f"{self.domain} ({self.record_type}) resolved to {values}, "
                    f"expected one of {self.expected}",
                    level="ERROR"
                )
                self.db_metrics.metric('matches_expected', 0.0)
                self.set_status('failed')
                return
            self.db_metrics.metric('matches_expected', 1.0)

        self.db_logger.write(
            f"{self.domain} ({self.record_type}) -> {values} (TTL {ttl})", level="INFO"
        )
        self.set_status('online')

    def _query(self):
        """Blocking resolution, run off the event loop via asyncio.to_thread."""
        resolver = self._make_resolver()
        return resolver.resolve(self.domain, self.record_type)

    def _store_values(self, values: List[str]):
        """Persist the current answer set as a Setting for the UI to display."""
        import json
        try:
            self.db.set_setting(f"dns_record:{self.id}", json.dumps(values))
        except Exception:
            pass

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import json
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                metric_name='resolved',
                title='RESOLUTION',
                on_text='OK',
                off_text='FAILED'
            )
        with layout.cell('type_card'):
            info_card('RECORD TYPE', self.record_type)
        with layout.cell('ttl_card'):
            ttl_label = info_card('TTL', '--')
        with layout.cell('answer'):
            answer_container = ui.element('div').style(
                'display: flex; flex-wrap: wrap; gap: 0.5rem; width: 100%'
            )
        with layout.cell('events'):
            self.internal_modules['ui']['events_table']()

        def update():
            ttl = self.latest_metric('ttl')
            if ttl is not None:
                ttl_label.text = f'{int(ttl.value)}s'

            raw = self.db.get_setting(f"dns_record:{self.id}")
            answer_container.clear()
            if not raw:
                return
            try:
                values = json.loads(raw)
            except Exception:
                return
            with answer_container:
                for v in values:
                    ok = self.expected is None or v in self.expected
                    color = STATUS_COLORS['online'] if ok else STATUS_COLORS['failed']
                    ui.label(v).classes('px-2 py-1 rounded text-sm font-mono').style(
                        f'background: {color}22; color: {color}'
                    )

        on_data_event(('metric', 'setting'), ttl_label, update)
