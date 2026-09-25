"""The webhook and ntfy channels: the request each notification becomes."""

import json

import httpx
import pytest

from vigil.core.notifications import build_channels
from vigil.core.notifications.channels import Message
from vigil.core.notifications.http import NtfyChannel, WebhookChannel, render

FAILED = Message('Nas is failed', 'Pool tank is DEGRADED\nHost: nas.lan', 'failed', 'problem',
                 'nas-zfs', 'https://vigil.lan/monitor/nas-zfs', 'Nas', 'nas.lan', 1790000000.0)
RECOVERED = Message('Nas recovered', 'Was down for 5 Minutes', 'online', 'recovered',
                    'nas-zfs', None, 'Nas', 'nas.lan', 1790000300.0)


def _capture(channel, status=200):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text='nope' if status >= 300 else 'ok')

    channel.transport = httpx.MockTransport(handler)
    return requests


class TestWebhook:
    async def test_default_payload_is_json(self):
        channel = WebhookChannel('hook', {'url': 'https://hooks.lan/vigil'})
        requests = _capture(channel)
        await channel.send(FAILED)

        [request] = requests
        assert (request.method, str(request.url)) == ('POST', 'https://hooks.lan/vigil')
        body = json.loads(request.content)
        assert body['title'] == 'Nas is failed'
        assert body['status'] == 'failed'
        assert body['kind'] == 'problem'
        assert body['monitor'] == {'id': 'nas-zfs', 'name': 'Nas', 'host': 'nas.lan'}
        assert body['url'] == 'https://vigil.lan/monitor/nas-zfs'
        assert body['timestamp'].startswith('2026-')

    async def test_a_mapping_payload_is_filled_and_sent_as_json(self):
        channel = WebhookChannel('hook', {
            'url': 'https://chat.lan/hook',
            'payload': {'text': '{title}: {body}', 'extra': ['{monitor_id}', 7], 'keep': '{nope}'},
        })
        requests = _capture(channel)
        await channel.send(FAILED)
        assert json.loads(requests[0].content) == {
            'text': 'Nas is failed: Pool tank is DEGRADED\nHost: nas.lan',
            'extra': ['nas-zfs', 7],
            'keep': '{nope}',
        }

    async def test_a_text_payload_is_sent_as_plain_text(self):
        channel = WebhookChannel('hook', {'url': 'https://x.lan', 'method': 'put',
                                          'payload': '{monitor_name} is {status}'})
        requests = _capture(channel)
        await channel.send(FAILED)
        assert requests[0].method == 'PUT'
        assert requests[0].content == b'Nas is failed'
        assert requests[0].headers['content-type'].startswith('text/plain')

    async def test_headers_and_a_bearer_token_file(self, tmp_path):
        token = tmp_path / 'token'
        token.write_text('s3cret\n')
        url = tmp_path / 'url'
        url.write_text('https://ha.lan/api/webhook/abc123\n')
        channel = WebhookChannel('hook', {'url_file': str(url), 'bearer_token_file': str(token),
                                          'headers': {'X-Source': 'vigil'}})
        requests = _capture(channel)
        await channel.send(FAILED)
        assert str(requests[0].url) == 'https://ha.lan/api/webhook/abc123'
        assert requests[0].headers['authorization'] == 'Bearer s3cret'
        assert requests[0].headers['x-source'] == 'vigil'

    async def test_a_rejected_request_raises(self):
        channel = WebhookChannel('hook', {'url': 'https://x.lan'})
        _capture(channel, status=500)
        with pytest.raises(RuntimeError, match='HTTP 500'):
            await channel.send(FAILED)

    def test_bad_settings_are_rejected(self, tmp_path):
        with pytest.raises(ValueError, match='needs `url`'):
            WebhookChannel('hook', {})
        with pytest.raises(ValueError, match='POST, PUT or PATCH'):
            WebhookChannel('hook', {'url': 'https://x', 'method': 'GET'})
        with pytest.raises(ValueError, match='could not read url_file'):
            WebhookChannel('hook', {'url_file': str(tmp_path / 'missing')})


class TestNtfy:
    async def test_publishes_to_the_topic_with_a_click_link(self):
        channel = NtfyChannel('phone', {'topic': 'vigil-alerts'})
        requests = _capture(channel)
        await channel.send(FAILED)

        [request] = requests
        assert str(request.url) == 'https://ntfy.sh'
        assert json.loads(request.content) == {
            'topic': 'vigil-alerts',
            'title': 'Nas is failed',
            'message': 'Pool tank is DEGRADED\nHost: nas.lan',
            'priority': 4,
            'tags': ['rotating_light'],
            'click': 'https://vigil.lan/monitor/nas-zfs',
        }

    async def test_recovery_uses_its_own_priority_and_tags(self):
        channel = NtfyChannel('phone', {'topic': 't'})
        requests = _capture(channel)
        await channel.send(RECOVERED)
        body = json.loads(requests[0].content)
        assert (body['priority'], body['tags']) == (2, ['white_check_mark'])
        assert 'click' not in body

    async def test_priority_and_tags_can_be_set_once_or_per_status(self):
        channel = NtfyChannel('phone', {'topic': 't', 'priority': {'failed': 'urgent'},
                                        'tags': 'server, vigil'})
        requests = _capture(channel)
        await channel.send(FAILED)
        await channel.send(RECOVERED)
        bodies = [json.loads(r.content) for r in requests]
        assert [(b['priority'], b['tags']) for b in bodies] == [
            (5, ['server', 'vigil']), (2, ['server', 'vigil'])]

    async def test_a_self_hosted_server_with_a_token(self, tmp_path):
        topic = tmp_path / 'topic'
        topic.write_text('private-topic\n')
        channel = NtfyChannel('phone', {'url': 'https://ntfy.lan/', 'topic_file': str(topic),
                                        'token': 'tk_abc'})
        requests = _capture(channel)
        await channel.send(FAILED)
        assert str(requests[0].url) == 'https://ntfy.lan'
        assert json.loads(requests[0].content)['topic'] == 'private-topic'
        assert requests[0].headers['authorization'] == 'Bearer tk_abc'

    async def test_username_and_password(self):
        channel = NtfyChannel('phone', {'topic': 't', 'username': 'me', 'password': 'pw'})
        requests = _capture(channel)
        await channel.send(FAILED)
        assert requests[0].headers['authorization'].startswith('Basic ')

    def test_bad_settings_are_rejected(self):
        with pytest.raises(ValueError, match='needs `topic`'):
            NtfyChannel('phone', {})
        with pytest.raises(ValueError, match='`priority` must be'):
            NtfyChannel('phone', {'topic': 't', 'priority': 9})
        with pytest.raises(ValueError, match='keys must be'):
            NtfyChannel('phone', {'topic': 't', 'tags': {'down': 'x'}})


def test_both_types_are_registered():
    channels = build_channels([
        {'id': 'hook', 'type': 'webhook', 'url': 'https://x'},
        {'id': 'phone', 'type': 'ntfy', 'topic': 't'},
    ], agents=None)
    assert {c: type(ch).__name__ for c, ch in channels.items()} == {
        'hook': 'WebhookChannel', 'phone': 'NtfyChannel'}


def test_render_leaves_non_strings_alone():
    assert render({'n': 1, 'b': True, 's': '{a}'}, {'a': 'x'}) == {'n': 1, 'b': True, 's': 'x'}
