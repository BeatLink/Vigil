"""Channels that deliver a notification with an HTTP request: a generic webhook, and ntfy."""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from vigil.core.notifications.channels import Channel, Message, per_status, secret

TIMEOUT_SECONDS = 10.0

_PLACEHOLDER = re.compile(r'\{(\w+)\}')


def fields(message: Message) -> Dict[str, str]:
    """The values a webhook payload template can use, by placeholder name."""
    return {
        'title': message.title,
        'body': message.body,
        'status': message.status,
        'kind': message.kind,
        'monitor_id': message.monitor_id,
        'monitor_name': message.monitor_name,
        'host': message.host,
        'url': message.url or '',
        'timestamp': datetime.fromtimestamp(message.timestamp).astimezone().isoformat(timespec='seconds')
        if message.timestamp else '',
    }


def render(template: Any, values: Dict[str, str]) -> Any:
    """Fill `{name}` placeholders in every string of a template; unknown names are left as they are."""
    if isinstance(template, str):
        return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)
    if isinstance(template, dict):
        return {key: render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [render(value, values) for value in template]
    return template


def default_payload(message: Message) -> Dict[str, Any]:
    """The JSON a webhook sends when it has no template of its own."""
    values = fields(message)
    return {
        'title': message.title,
        'body': message.body,
        'status': message.status,
        'kind': message.kind,
        'monitor': {'id': message.monitor_id, 'name': message.monitor_name, 'host': message.host},
        'url': message.url,
        'timestamp': values['timestamp'],
    }


class HttpChannel(Channel):
    """A channel that sends one HTTP request per notification."""

    transport: Optional[httpx.AsyncBaseTransport] = None

    async def request(self, method: str, url: str, **kwargs: Any) -> None:
        """Send the request, raising if it fails or the server does not accept it."""
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self.transport) as client:
            response = await client.request(method, url, **kwargs)
        if response.status_code >= 300:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")


class WebhookChannel(HttpChannel):
    """Sends each notification to a URL, as JSON or as the payload template given."""
    TYPE = "webhook"

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.url = secret(config, 'url')
        if not self.url:
            raise ValueError("a webhook channel needs `url` or `url_file`")
        self.method = str(config.get('method', 'POST')).upper()
        if self.method not in ('POST', 'PUT', 'PATCH'):
            raise ValueError(f"`method` must be POST, PUT or PATCH, not {self.method!r}")
        headers = config.get('headers') or {}
        if not isinstance(headers, dict):
            raise ValueError("`headers` must be a mapping of header names to values")
        self.headers = {str(k): str(v) for k, v in headers.items()}
        token = secret(config, 'bearer_token')
        if token:
            self.headers['Authorization'] = f"Bearer {token}"
        self.payload = config.get('payload')

    async def send(self, message: Message) -> None:
        if self.payload is None:
            await self.request(self.method, self.url, json=default_payload(message), headers=self.headers)
            return
        body = render(self.payload, fields(message))
        if isinstance(body, str):
            headers = {'Content-Type': 'text/plain; charset=utf-8', **self.headers}
            await self.request(self.method, self.url, content=body.encode('utf-8'), headers=headers)
        else:
            await self.request(self.method, self.url, json=body, headers=self.headers)


NTFY_PRIORITIES = {'min': 1, 'low': 2, 'default': 3, 'high': 4, 'max': 5, 'urgent': 5}


def _priority(value: Any) -> int:
    """An ntfy priority given by name or as 1 to 5."""
    if isinstance(value, int) and 1 <= value <= 5:
        return value
    if str(value).lower() in NTFY_PRIORITIES:
        return NTFY_PRIORITIES[str(value).lower()]
    raise ValueError(f"`priority` must be 1-5 or one of {', '.join(NTFY_PRIORITIES)}, not {value!r}")


def _tags(value: Any) -> List[str]:
    """ntfy tags given as a list or as one comma-separated string."""
    if isinstance(value, list):
        return [str(tag) for tag in value]
    return [tag.strip() for tag in str(value).split(',') if tag.strip()]


class NtfyChannel(HttpChannel):
    """Publishes each notification to an ntfy topic; tapping it opens the monitor's page."""
    TYPE = "ntfy"

    DEFAULT_URL = "https://ntfy.sh"
    DEFAULT_PRIORITY = {'failed': 'high', 'warning': 'default', 'unavailable': 'default',
                        'recovered': 'low', 'flapping': 'default'}
    DEFAULT_TAGS = {'failed': 'rotating_light', 'warning': 'warning',
                    'unavailable': 'grey_question', 'recovered': 'white_check_mark',
                    'flapping': 'repeat'}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.url = str(config.get('url') or self.DEFAULT_URL).rstrip('/')
        self.topic = secret(config, 'topic')
        if not self.topic:
            raise ValueError("an ntfy channel needs `topic` or `topic_file`")
        self.headers: Dict[str, str] = {}
        token = secret(config, 'token')
        password = secret(config, 'password')
        if token:
            self.headers['Authorization'] = f"Bearer {token}"
        self.auth = (str(config['username']), password or '') if config.get('username') else None
        self.priority = {key: _priority(value) for key, value in
                         per_status(config.get('priority'), self.DEFAULT_PRIORITY, 'priority').items()}
        self.tags = {key: _tags(value) for key, value in
                     per_status(config.get('tags'), self.DEFAULT_TAGS, 'tags').items()}

    async def send(self, message: Message) -> None:
        publish: Dict[str, Any] = {
            'topic': self.topic,
            'title': message.title,
            'message': message.body or message.title,
            'priority': self.priority.get(message.key, 3),
            'tags': self.tags.get(message.key, []),
        }
        if message.url:
            publish['click'] = message.url
        await self.request('POST', self.url, content=json.dumps(publish).encode('utf-8'),
                           headers={'Content-Type': 'application/json', **self.headers},
                           auth=self.auth)

