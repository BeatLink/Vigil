from typing import Any, Callable, Dict, List, Optional

import dns.exception
import dns.resolver

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

_DEFAULT_LAYOUT = [
    ['status_card', 'type_card', 'ttl_card'],
    ['answer'],
    ['events'],
]


def _answer_to_str(record_type: str, rdata) -> str:
    if record_type == 'MX':
        return f"{rdata.preference} {str(rdata.exchange).rstrip('.')}".strip()
    if record_type == 'TXT':
        return ''.join(part.decode('utf-8', 'replace') if isinstance(part, bytes) else part
                       for part in rdata.strings)
    return str(rdata).rstrip('.')


class DnsRecordCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.domain = config.get('domain')
        self.record_type = str(config.get('record_type', 'A')).upper()
        self.resolver_addr = config.get('resolver')
        self.port = int(config.get('port', 53))
        self.dns_timeout = float(config.get('timeout', 5))
        expected = config.get('expected')
        self.expected: Optional[List[str]] = (
            [str(v).rstrip('.') for v in expected] if expected else None
        )
        self.target = self.domain or self.name

    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def _make_resolver(self) -> "dns.resolver.Resolver":
        resolver = dns.resolver.Resolver(configure=self.resolver_addr is None)
        if self.resolver_addr:
            resolver.nameservers = [self.resolver_addr]
        resolver.port = self.port
        resolver.timeout = self.dns_timeout
        resolver.lifetime = self.dns_timeout
        return resolver

    def _query(self):
        try:
            resolver = self._make_resolver()
            return ('ok', resolver.resolve(self.domain, self.record_type))
        except dns.resolver.NXDOMAIN:
            return ('nxdomain', None)
        except dns.resolver.NoAnswer:
            return ('no_answer', None)
        except dns.exception.Timeout:
            return ('timeout', None)
        except dns.exception.DNSException as e:
            return ('dns_error', str(e))

    def local_call(self) -> Optional[Callable[[], Any]]:
        if not self.domain:
            return lambda: ('no_domain', None)
        return self._query

    def parse_local(self, result: Any) -> CollectResult:
        kind, payload = result

        if kind == 'no_domain':
            return CollectResult.failed("No 'domain' configured")
        if kind == 'nxdomain':
            return CollectResult(
                metrics={'resolved': 0.0},
                logs=[(f"{self.domain} does not exist (NXDOMAIN)", "ERROR")],
                status='failed',
            )
        if kind == 'no_answer':
            return CollectResult(
                metrics={'resolved': 0.0},
                logs=[(f"{self.domain} has no {self.record_type} record", "ERROR")],
                status='failed',
            )
        if kind == 'timeout':
            return CollectResult(
                metrics={'resolved': 0.0},
                logs=[(
                    f"Query for {self.domain} ({self.record_type}) timed out after {self.dns_timeout}s",
                    "ERROR",
                )],
                status='failed',
            )
        if kind == 'dns_error':
            return CollectResult(
                metrics={'resolved': 0.0},
                logs=[(f"DNS query failed: {payload}", "ERROR")],
                status='failed',
            )

        answers = payload
        values = [_answer_to_str(self.record_type, r) for r in answers]
        ttl = answers.rrset.ttl if answers.rrset is not None else None

        import json
        metrics = {'resolved': 1.0}
        if ttl is not None:
            metrics['ttl'] = float(ttl)
        settings = {f"dns_record:{self.id}": json.dumps(values)}

        if self.expected is not None:
            unexpected = [v for v in values if v not in self.expected]
            if unexpected:
                metrics['matches_expected'] = 0.0
                return CollectResult(
                    metrics=metrics,
                    logs=[(
                        f"{self.domain} ({self.record_type}) resolved to {values}, "
                        f"expected one of {self.expected}",
                        "ERROR",
                    )],
                    status='failed',
                    settings=settings,
                )
            metrics['matches_expected'] = 1.0

        return CollectResult(
            metrics=metrics,
            logs=[(f"{self.domain} ({self.record_type}) -> {values} (TTL {ttl})", "INFO")],
            status='online',
            settings=settings,
        )


class DnsRecordUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.record_type = str(config.get('record_type', 'A')).upper()
        expected = config.get('expected')
        self.expected: Optional[List[str]] = (
            [str(v).rstrip('.') for v in expected] if expected else None
        )

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import json
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page(metric_names=[])

        with layout.cell('status_card'):
            self.ui.status_card(
                page,
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
            self.ui.events_table(page)

        def update():
            ttl = self.storage.latest_metric('ttl')
            if ttl is not None:
                ttl_label.text = f'{int(ttl.value)}s'

            raw = self.storage.get_setting(f"dns_record:{self.id}")
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

        page.on_refresh(update)
        update()
        page.start()
