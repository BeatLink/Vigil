"""Existence and correctness of one DNS record, resolved through a DnsQuery
request issued from the Vigil host. Config: domain, record_type, resolver,
port, timeout, expected (list of acceptable answer values). NXDOMAIN, no
answer, a query timeout or error, or any answer outside the expected list is
failed; a resolving record, matching expected when that is set, is online.
This monitor has no warning tier."""

import json
from typing import Any, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import (
    CollectResult, DnsQuery, DnsResult, Request, Result,
)

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


def _failed_lookup(kind: str, error: Optional[str], domain: str, record_type: str,
                   dns_timeout: float) -> Optional[CollectResult]:
    """The failed CollectResult for a non-answer query kind, or None when the query succeeded."""
    messages = {
        'nxdomain': f"{domain} does not exist (NXDOMAIN)",
        'no_answer': f"{domain} has no {record_type} record",
        'timeout': f"Query for {domain} ({record_type}) timed out after {dns_timeout}s",
        'dns_error': f"DNS query failed: {error}",
    }
    message = messages.get(kind)
    if message is None:
        return None
    return CollectResult(
        metrics={'resolved': 0.0},
        logs=[(message, "ERROR")],
        status='failed',
    )


class DnsRecord(Plugin):
    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
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

        from vigil.core.ui.spec import register_item_color_rule
        self._color_rule_name = f'dns_record_expected_{self.id}'
        register_item_color_rule(self._color_rule_name)(self._item_color)

    def requests(self) -> List[Request]:
        if not self.domain:
            return []
        return [DnsQuery(
            domain=self.domain,
            record_type=self.record_type,
            resolver=self.resolver_addr,
            port=self.port,
            timeout=self.dns_timeout,
        )]

    def parse_results(self, results: List[Result]) -> CollectResult:
        """Turns the single DnsResult into a CollectResult with resolved/ttl/match
        metrics, the answer values stored as a setting, and a status that is
        failed for NXDOMAIN, no answer, timeout, query error, or any answer
        outside the expected list."""
        if not results:
            return CollectResult.failed("No 'domain' configured")

        result: DnsResult = results[0]
        failure = _failed_lookup(result.kind, result.error, self.domain,
                                 self.record_type, self.dns_timeout)
        if failure is not None:
            return failure

        answers = result.answer
        values = [_answer_to_str(self.record_type, rdata) for rdata in answers]
        ttl = answers.rrset.ttl if answers.rrset is not None else None

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

    def _item_color(self, item: Dict[str, Any]) -> str:
        value = item.get('value')
        return 'online' if self.expected is None or value in self.expected else 'failed'

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

