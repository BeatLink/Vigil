"""Channels that hand a notification to another delivery system: email over SMTP, and Apprise."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, List

from vigil.core.notifications.channels import Channel, Message, secret

TIMEOUT_SECONDS = 20


def _addresses(value: Any) -> List[str]:
    """Recipients given as a list or as one comma-separated string."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or '').split(',') if v.strip()]


def text(message: Message) -> str:
    """A notification's body with its link on a line of its own."""
    return "\n\n".join(part for part in (message.body, message.url or '') if part)


class SmtpChannel(Channel):
    """Emails each notification."""
    TYPE = "smtp"

    SECURITY_PORTS = {'starttls': 587, 'tls': 465, 'none': 25}

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        self.host = str(config.get('host') or '')
        if not self.host:
            raise ValueError("an smtp channel needs `host`")
        self.security = str(config.get('security', 'starttls')).lower()
        if self.security not in self.SECURITY_PORTS:
            raise ValueError(f"`security` must be starttls, tls or none, not {self.security!r}")
        self.port = int(config.get('port') or self.SECURITY_PORTS[self.security])
        self.username = str(config['username']) if config.get('username') else None
        self.password = secret(config, 'password')
        self.sender = str(config.get('from') or self.username or '')
        if not self.sender:
            raise ValueError("an smtp channel needs `from` (or a `username` to send as)")
        self.recipients = _addresses(config.get('to'))
        if not self.recipients:
            raise ValueError("an smtp channel needs `to`, one address or a list")
        self.subject_prefix = str(config.get('subject_prefix', ''))

    def email(self, message: Message) -> EmailMessage:
        """The email one notification becomes."""
        mail = EmailMessage()
        mail['Subject'] = f"{self.subject_prefix}{message.title}"
        mail['From'] = self.sender
        mail['To'] = ', '.join(self.recipients)
        mail.set_content(text(message))
        return mail

    def _deliver(self, mail: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.security == 'tls':
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_SECONDS)
        with server:
            if self.security == 'starttls':
                server.starttls(context=context)
            if self.username and self.password:
                server.login(self.username, self.password)
            server.send_message(mail)

    async def send(self, message: Message) -> None:
        await asyncio.to_thread(self._deliver, self.email(message))


class AppriseChannel(Channel):
    """Sends each notification through Apprise, which reaches most chat, push and SMS services."""
    TYPE = "apprise"

    def __init__(self, channel_id: str, config: Dict[str, Any], agents: Any = None):
        super().__init__(channel_id, config)
        try:
            import apprise
        except ImportError as e:
            raise ValueError("an apprise channel needs the apprise package: pip install 'vigil[apprise]'") from e
        self._apprise = apprise
        inline = config.get('urls') or []
        urls = [str(u) for u in (inline if isinstance(inline, list) else [inline])]
        if config.get('urls_file'):
            listed = secret({'urls_file': config['urls_file']}, 'urls') or ''
            urls += [line.strip() for line in listed.splitlines()
                     if line.strip() and not line.strip().startswith('#')]
        if not urls:
            raise ValueError("an apprise channel needs `urls` or `urls_file`")
        self._client = apprise.Apprise()
        for url in urls:
            if not self._client.add(url):
                raise ValueError("an apprise URL was not recognised; check its scheme and format")

    def notify_type(self, message: Message) -> str:
        """Apprise's severity for a notification, which some services show as a color or icon."""
        types = self._apprise.NotifyType
        if message.clears:
            return types.SUCCESS
        if message.kind == 'flapping':
            return types.WARNING
        return {'failed': types.FAILURE, 'warning': types.WARNING}.get(message.status, types.INFO)

    async def send(self, message: Message) -> None:
        delivered = await asyncio.to_thread(
            self._client.notify, body=text(message) or message.title, title=message.title,
            notify_type=self.notify_type(message),
        )
        if not delivered:
            raise RuntimeError("Apprise could not deliver to one or more of its URLs")
