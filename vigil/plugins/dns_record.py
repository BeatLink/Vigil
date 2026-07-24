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

        from vigil.web.ui.spec import register_item_color_rule
        self._color_rule_name = f'dns_record_expected_{self.id}'
        register_item_color_rule(self._color_rule_name)(self._item_color)

    def _item_color(self, item: Dict[str, Any]) -> str:
        v = item.get('value')
        return 'online' if self.expected is None or v in self.expected else 'failed'

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'status_card': {'metric': 'resolved', 'title': 'RESOLUTION',
                                'on_text': 'OK', 'off_text': 'FAILED'},
                'type_card': {'title': 'RECORD TYPE', 'value': self.record_type},
                'ttl_card': {'metric': 'ttl', 'title': 'TTL', 'format': 'ttl_seconds'},
                'answer': {
                    'repeat': {
                        'source': 'setting',
                        'setting_key': 'dns_record:{plugin_id}',
                        'item_label': '_none',
                        'item_value': 'value',
                        'item_color_by': self._color_rule_name,
                        'container': 'chips',
                        'empty_text': 'No answer yet',
                    },
                },
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
