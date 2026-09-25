"""The smtp and apprise channels: the email or Apprise call each notification becomes."""

import pytest

from vigil.core.notifications import build_channels
from vigil.core.notifications.channels import Message
from vigil.core.notifications.mail import AppriseChannel, SmtpChannel

FAILED = Message('Nas is failed', 'Pool tank is DEGRADED', 'failed', 'problem',
                 'nas-zfs', 'https://vigil.lan/monitor/nas-zfs', 'Nas', 'nas.lan', 1790000000.0)
RECOVERED = Message('Nas recovered', 'Was down for 5 Minutes', 'online', 'recovered', 'nas-zfs')


class FakeSmtp:
    """Records what one SMTP session did."""
    sessions = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ssl = host, port, context is not None
        self.did = []
        FakeSmtp.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.did.append('starttls')

    def login(self, user, password):
        self.did.append(('login', user, password))

    def send_message(self, mail):
        self.did.append(('send', mail))


@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.sessions = []
    monkeypatch.setattr('smtplib.SMTP', FakeSmtp)
    monkeypatch.setattr('smtplib.SMTP_SSL', FakeSmtp)
    return FakeSmtp.sessions


class TestSmtp:
    async def test_starttls_login_and_send(self, smtp, tmp_path):
        password = tmp_path / 'pw'
        password.write_text('s3cret\n')
        channel = SmtpChannel('mail', {'host': 'mail.lan', 'username': 'vigil@lan',
                                       'password_file': str(password), 'to': 'me@lan, you@lan',
                                       'subject_prefix': '[Vigil] '})
        await channel.send(FAILED)

        [session] = smtp
        assert (session.host, session.port) == ('mail.lan', 587)
        assert session.did[:2] == ['starttls', ('login', 'vigil@lan', 's3cret')]
        mail = session.did[2][1]
        assert mail['Subject'] == '[Vigil] Nas is failed'
        assert (mail['From'], mail['To']) == ('vigil@lan', 'me@lan, you@lan')
        assert mail.get_content() == 'Pool tank is DEGRADED\n\nhttps://vigil.lan/monitor/nas-zfs\n'

    async def test_implicit_tls_uses_port_465(self, smtp):
        channel = SmtpChannel('mail', {'host': 'mail.lan', 'security': 'tls',
                                       'from': 'vigil@lan', 'to': ['me@lan']})
        await channel.send(RECOVERED)
        assert (smtp[0].port, smtp[0].ssl, smtp[0].did[0][0]) == (465, True, 'send')

    async def test_no_security_sends_without_starttls_or_login(self, smtp):
        channel = SmtpChannel('mail', {'host': 'relay.lan', 'security': 'none', 'port': 2525,
                                       'from': 'vigil@lan', 'to': 'me@lan'})
        await channel.send(FAILED)
        assert smtp[0].port == 2525
        assert [d[0] for d in smtp[0].did] == ['send']

    def test_bad_settings_are_rejected(self):
        with pytest.raises(ValueError, match='needs `host`'):
            SmtpChannel('mail', {'to': 'a@b'})
        with pytest.raises(ValueError, match='needs `from`'):
            SmtpChannel('mail', {'host': 'h', 'to': 'a@b'})
        with pytest.raises(ValueError, match='needs `to`'):
            SmtpChannel('mail', {'host': 'h', 'from': 'a@b'})
        with pytest.raises(ValueError, match='starttls, tls or none'):
            SmtpChannel('mail', {'host': 'h', 'from': 'a@b', 'to': 'c@d', 'security': 'ssl'})


class TestApprise:
    def _channel(self, monkeypatch, config, delivered=True):
        channel = AppriseChannel('bridge', config)
        calls = []

        def notify(**kwargs):
            calls.append(kwargs)
            return delivered

        monkeypatch.setattr(channel._client, 'notify', notify)
        return channel, calls

    async def test_sends_title_body_link_and_severity(self, monkeypatch):
        channel, calls = self._channel(monkeypatch, {'urls': 'json://hooks.lan/vigil'})
        await channel.send(FAILED)
        await channel.send(RECOVERED)
        assert calls[0] == {'title': 'Nas is failed',
                            'body': 'Pool tank is DEGRADED\n\nhttps://vigil.lan/monitor/nas-zfs',
                            'notify_type': 'failure'}
        assert calls[1]['notify_type'] == 'success'

    async def test_urls_file_holds_one_url_per_line(self, monkeypatch, tmp_path):
        urls = tmp_path / 'urls'
        urls.write_text('# chat\njson://a.lan/x\n\njson://b.lan/y?a=1,2\n')
        channel, _ = self._channel(monkeypatch, {'urls': ['json://c.lan/z'], 'urls_file': str(urls)})
        assert len(channel._client) == 3

    async def test_a_failed_delivery_raises(self, monkeypatch):
        channel, _ = self._channel(monkeypatch, {'urls': 'json://hooks.lan/vigil'}, delivered=False)
        with pytest.raises(RuntimeError, match='could not deliver'):
            await channel.send(FAILED)

    def test_bad_settings_are_rejected(self):
        with pytest.raises(ValueError, match='needs `urls`'):
            AppriseChannel('bridge', {})
        with pytest.raises(ValueError, match='not recognised'):
            AppriseChannel('bridge', {'urls': 'notaservice://x'})


def test_both_types_are_registered():
    channels = build_channels([
        {'id': 'mail', 'type': 'smtp', 'host': 'h', 'from': 'a@b', 'to': 'c@d'},
        {'id': 'bridge', 'type': 'apprise', 'urls': 'json://hooks.lan/vigil'},
    ], agents=None)
    assert {c: type(ch).__name__ for c, ch in channels.items()} == {
        'mail': 'SmtpChannel', 'bridge': 'AppriseChannel'}
