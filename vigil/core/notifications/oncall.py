"""On-call channels: PagerDuty and Opsgenie, which open one alert per monitor and close it on recovery."""

from typing import Any, Dict, List
from urllib.parse import quote

from vigil.core.notifications.channels import Message, per_status, secret
from vigil.core.notifications.http import HttpChannel

TEST_KEY = "vigil-test"
"""The alert key a test notification uses, since it belongs to no monitor."""


def _key(message: Message) -> str:
    """The alert key that ties a monitor's problem to its recovery."""
    return message.monitor_id or TEST_KEY


class OnCallChannel(HttpChannel):
    """An on-call service that keeps one open alert per monitor."""

    dismisses_on_recovery = True

    async def open_alert(self, message: Message) -> None:
        raise NotImplementedError

    async def close_alert(self, message: Message) -> None:
        raise NotImplementedError

    async def send(self, message: Message) -> None:
        if message.clears:
            await self.close_alert(message)
            return
        await self.open_alert(message)
        if message.kind == "test":
            # A test proves both halves and leaves nothing open.
            await self.close_alert(message)

    async def send_batch(self, messages: List[Message], summary: Message) -> None:
        """Each monitor keeps its own alert, so a batch is sent one by one."""
        for message in messages:
            await self.send(message)


class PagerDutyChannel(OnCallChannel):
    """Triggers and resolves PagerDuty incidents through the Events API v2."""
    TYPE = "pagerduty"

    DEFAULT_URL = "https://events.pagerduty.com/v2/enqueue"
    SEVERITIES = ('critical', 'error', 'warning', 'info')
    DEFAULT_SEVERITY = {'failed': 'critical', 'warning': 'warning', 'unavailable': 'error',
                        'recovered': 'info', 'flapping': 'warning'}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.routing_key = secret(config, 'routing_key')
        if not self.routing_key:
            raise ValueError("a pagerduty channel needs `routing_key` or `routing_key_file`")
        self.url = str(config.get('url') or self.DEFAULT_URL)
        self.severity = per_status(config.get('severity'), self.DEFAULT_SEVERITY, 'severity')
        bad = {str(v) for v in self.severity.values()} - set(self.SEVERITIES)
        if bad:
            raise ValueError(f"`severity` must be critical, error, warning or info, not {sorted(bad)}")

    async def _event(self, action: str, message: Message, extra: Dict[str, Any]) -> None:
        await self.request('POST', self.url, json={
            'routing_key': self.routing_key, 'event_action': action,
            'dedup_key': _key(message), **extra,
        })

    async def open_alert(self, message: Message) -> None:
        payload: Dict[str, Any] = {
            'summary': message.title[:1024],
            'source': message.host or 'vigil',
            'severity': str(self.severity.get(message.key, 'info')),
            'custom_details': {'details': message.body, 'status': message.status},
        }
        if message.monitor_name:
            payload['component'] = message.monitor_name
        extra: Dict[str, Any] = {'payload': payload}
        if message.url:
            extra['links'] = [{'href': message.url, 'text': 'Open in Vigil'}]
        await self._event('trigger', message, extra)

    async def close_alert(self, message: Message) -> None:
        await self._event('resolve', message, {})


class OpsgenieChannel(OnCallChannel):
    """Creates and closes Opsgenie alerts through the Alert API."""
    TYPE = "opsgenie"

    REGIONS = {'us': 'https://api.opsgenie.com', 'eu': 'https://api.eu.opsgenie.com'}
    PRIORITIES = ('P1', 'P2', 'P3', 'P4', 'P5')
    DEFAULT_PRIORITY = {'failed': 'P1', 'warning': 'P3', 'unavailable': 'P3',
                        'recovered': 'P5', 'flapping': 'P3'}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.api_key = secret(config, 'api_key')
        if not self.api_key:
            raise ValueError("an opsgenie channel needs `api_key` or `api_key_file`")
        region = str(config.get('region', 'us')).lower()
        if region not in self.REGIONS and not config.get('url'):
            raise ValueError(f"`region` must be us or eu, not {region!r}")
        self.url = str(config.get('url') or self.REGIONS[region]).rstrip('/')
        self.priority = per_status(config.get('priority'), self.DEFAULT_PRIORITY, 'priority')
        bad = {str(v) for v in self.priority.values()} - set(self.PRIORITIES)
        if bad:
            raise ValueError(f"`priority` must be P1 to P5, not {sorted(bad)}")
        self.headers = {'Authorization': f"GenieKey {self.api_key}"}

    async def open_alert(self, message: Message) -> None:
        details = {'status': message.status}
        if message.url:
            details['url'] = message.url
        alert = {
            'message': message.title[:130],
            'alias': _key(message),
            'description': message.body[:15000],
            'priority': str(self.priority.get(message.key, 'P3')),
            'source': 'Vigil',
            'details': details,
        }
        if message.host:
            alert['entity'] = message.host
        await self.request('POST', f"{self.url}/v2/alerts", headers=self.headers, json=alert)

    async def close_alert(self, message: Message) -> None:
        await self.request('POST', f"{self.url}/v2/alerts/{quote(_key(message), safe='')}/close",
                           params={'identifierType': 'alias'}, headers=self.headers,
                           json={'source': 'Vigil', 'note': message.title})
