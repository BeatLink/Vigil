"""PagerDuty and Opsgenie: one alert per monitor, opened on a problem and closed on recovery."""

import json

import httpx
import pytest

from vigil.core.notifications import build_channels
from vigil.core.notifications.channels import Message, digest
from vigil.core.notifications.oncall import OpsgenieChannel, PagerDutyChannel

FAILED = Message('Nas is failed', 'Pool tank is DEGRADED', 'failed', 'problem', 'nas/zfs',
                 'https://vigil.lan/monitor/nas%2Fzfs', 'Nas', 'nas.lan', 1790000000.0)
RECOVERED = Message('Nas recovered', 'Was down for 5 Minutes', 'online', 'recovered', 'nas/zfs')
FLAPPING = Message('Nas is flapping', 'b', 'online', 'flapping', 'nas/zfs')
TEST = Message('Vigil test notification', 'b', 'online', 'test')


def _capture(channel):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json={'status': 'success'})

    channel.transport = httpx.MockTransport(handler)
    return requests


def _bodies(requests):
    return [json.loads(r.content) for r in requests]


class TestPagerDuty:
    async def test_a_problem_triggers_and_a_recovery_resolves_the_same_incident(self):
        channel = PagerDutyChannel('pd', {'routing_key': 'R0UT1NG'})
        requests = _capture(channel)
        await channel.send(FAILED)
        await channel.send(RECOVERED)

        trigger, resolve = _bodies(requests)
        assert str(requests[0].url) == 'https://events.pagerduty.com/v2/enqueue'
        assert trigger == {
            'routing_key': 'R0UT1NG', 'event_action': 'trigger', 'dedup_key': 'nas/zfs',
            'payload': {'summary': 'Nas is failed', 'source': 'nas.lan', 'severity': 'critical',
                        'component': 'Nas',
                        'custom_details': {'details': 'Pool tank is DEGRADED', 'status': 'failed'}},
            'links': [{'href': 'https://vigil.lan/monitor/nas%2Fzfs', 'text': 'Open in Vigil'}],
        }
        assert resolve == {'routing_key': 'R0UT1NG', 'event_action': 'resolve', 'dedup_key': 'nas/zfs'}

    async def test_severity_per_status_and_flapping(self, tmp_path):
        key = tmp_path / 'key'
        key.write_text('R0UT1NG\n')
        channel = PagerDutyChannel('pd', {'routing_key_file': str(key), 'severity': {'failed': 'error'}})
        requests = _capture(channel)
        await channel.send(FAILED)
        await channel.send(FLAPPING)
        assert [b['payload']['severity'] for b in _bodies(requests)] == ['error', 'warning']

    async def test_a_test_opens_and_closes_its_own_incident(self):
        channel = PagerDutyChannel('pd', {'routing_key': 'k'})
        requests = _capture(channel)
        await channel.send(TEST)
        assert [(b['event_action'], b['dedup_key']) for b in _bodies(requests)] == [
            ('trigger', 'vigil-test'), ('resolve', 'vigil-test')]

    async def test_a_batch_keeps_one_incident_per_monitor(self):
        channel = PagerDutyChannel('pd', {'routing_key': 'k'})
        requests = _capture(channel)
        other = Message('Web is failed', 'b', 'failed', 'problem', 'web')
        await channel.send_batch([FAILED, other], digest([FAILED, other], None))
        assert [b['dedup_key'] for b in _bodies(requests)] == ['nas/zfs', 'web']

    def test_bad_settings_are_rejected(self):
        with pytest.raises(ValueError, match='routing_key'):
            PagerDutyChannel('pd', {})
        with pytest.raises(ValueError, match='critical, error, warning or info'):
            PagerDutyChannel('pd', {'routing_key': 'k', 'severity': 'high'})


class TestOpsgenie:
    async def test_a_problem_creates_and_a_recovery_closes_the_alert_by_alias(self):
        channel = OpsgenieChannel('og', {'api_key': 'G3N13'})
        requests = _capture(channel)
        await channel.send(FAILED)
        await channel.send(RECOVERED)

        create, close = requests
        assert str(create.url) == 'https://api.opsgenie.com/v2/alerts'
        assert create.headers['authorization'] == 'GenieKey G3N13'
        assert json.loads(create.content) == {
            'message': 'Nas is failed', 'alias': 'nas/zfs', 'description': 'Pool tank is DEGRADED',
            'priority': 'P1', 'source': 'Vigil', 'entity': 'nas.lan',
            'details': {'status': 'failed', 'url': 'https://vigil.lan/monitor/nas%2Fzfs'},
        }
        assert str(close.url) == 'https://api.opsgenie.com/v2/alerts/nas%2Fzfs/close?identifierType=alias'
        assert json.loads(close.content) == {'source': 'Vigil', 'note': 'Nas recovered'}

    async def test_eu_region_and_priorities(self):
        channel = OpsgenieChannel('og', {'api_key': 'k', 'region': 'EU', 'priority': {'failed': 'P2'}})
        requests = _capture(channel)
        await channel.send(FAILED)
        assert str(requests[0].url).startswith('https://api.eu.opsgenie.com/')
        assert json.loads(requests[0].content)['priority'] == 'P2'

    def test_bad_settings_are_rejected(self):
        with pytest.raises(ValueError, match='api_key'):
            OpsgenieChannel('og', {})
        with pytest.raises(ValueError, match='us or eu'):
            OpsgenieChannel('og', {'api_key': 'k', 'region': 'mars'})
        with pytest.raises(ValueError, match='P1 to P5'):
            OpsgenieChannel('og', {'api_key': 'k', 'priority': 'urgent'})


def test_both_types_are_registered():
    channels = build_channels([
        {'id': 'pd', 'type': 'pagerduty', 'routing_key': 'k'},
        {'id': 'og', 'type': 'opsgenie', 'api_key': 'k'},
    ], agents=None)
    assert {c: type(ch).__name__ for c, ch in channels.items()} == {
        'pd': 'PagerDutyChannel', 'og': 'OpsgenieChannel'}
