"""Notifications: which status changes notify, the message sent, and delivery."""

import asyncio
from types import SimpleNamespace

import pytest

from vigil.core.connectors.agent_connector import AgentConnection, AgentRegistry
from vigil.core.notifications import NotificationEngine, build_channels, monitor_url
from vigil.core.notifications.channels import DesktopChannel, Message
from vigil.core.notifications.rules import (
    PROBLEM, RECOVERED, REMINDER, Alert, Rule, Tracker, resolve_rules,
)
from vigil_agent import protocol as proto


def _plugin(plugin_id, children=(), type_='cpu', **config):
    return SimpleNamespace(id=plugin_id, name=plugin_id.title(), target='host.lan',
                           config={'type': type_, **config}, children=list(children))


class TestResolveRules:
    def test_every_monitor_gets_the_defaults(self):
        rules = resolve_rules([_plugin('a'), _plugin('b')], {}, ['desk'])
        assert rules == {'a': Rule(('desk',)), 'b': Rule(('desk',))}

    def test_group_settings_pass_to_children_but_the_group_never_notifies(self):
        group = _plugin('g', [_plugin('a'), _plugin('b', notify={'after': 3})],
                        type_='group', notify={'on': ['failed', 'warning']})
        rules = resolve_rules([group], {}, ['desk'])
        assert set(rules) == {'a', 'b'}
        assert rules['a'].on == {'failed', 'warning'}
        assert (rules['b'].on, rules['b'].after) == ({'failed', 'warning'}, 3)

    def test_notify_false_mutes_a_monitor_and_its_children(self):
        group = _plugin('g', [_plugin('a'), _plugin('b', notify=True)], type_='group', notify=False)
        assert resolve_rules([group, _plugin('c')], {}, ['desk']).keys() == {'c'}

    def test_a_child_mapping_switches_a_muted_group_back_on(self):
        group = _plugin('g', [_plugin('a', notify={'after': 2})], type_='group', notify=False)
        assert resolve_rules([group], {}, ['desk'])['a'].after == 2

    def test_unknown_channels_and_statuses_are_dropped(self):
        rules = resolve_rules([_plugin('a', notify={'channels': ['nope', 'desk'],
                                                    'on': ['failed', 'bogus']})], {}, ['desk'])
        assert rules['a'] == Rule(('desk',), frozenset({'failed'}))

    def test_repeat_takes_a_duration(self):
        rules = resolve_rules([_plugin('a')], {'repeat': '1h'}, ['desk'])
        assert rules['a'].repeat == 3600

    def test_no_channels_means_no_rules(self):
        assert resolve_rules([_plugin('a')], {}, []) == {}


class TestTracker:
    def _tracker(self, **rule):
        return Tracker({'m': Rule(('desk',), **rule)})

    def test_failure_then_recovery(self):
        t = self._tracker()
        assert t.observe('m', 'online', 0) is None
        assert t.observe('m', 'failed', 10) == Alert(PROBLEM, 'failed', 10)
        assert t.observe('m', 'failed', 20) is None
        assert t.observe('m', 'online', 30) == Alert(RECOVERED, 'online', 10)
        assert t.observe('m', 'online', 40) is None

    def test_statuses_outside_on_are_ignored(self):
        t = self._tracker()
        assert t.observe('m', 'warning', 0) is None
        assert t.observe('m', 'unavailable', 1) is None

    def test_after_waits_for_consecutive_problem_cycles(self):
        t = self._tracker(after=3)
        assert t.observe('m', 'failed', 0) is None
        assert t.observe('m', 'failed', 1) is None
        assert t.observe('m', 'online', 2) is None  # a blip resets the count, and is no recovery
        assert t.observe('m', 'failed', 3) is None
        assert t.observe('m', 'failed', 4) is None
        assert t.observe('m', 'failed', 5) == Alert(PROBLEM, 'failed', 3)

    def test_repeat_reminds_while_the_problem_lasts(self):
        t = self._tracker(repeat=60)
        t.observe('m', 'failed', 0)
        assert t.observe('m', 'failed', 59) is None
        assert t.observe('m', 'failed', 60) == Alert(REMINDER, 'failed', 0)
        assert t.observe('m', 'failed', 100) is None

    def test_escalation_notifies_again(self):
        t = self._tracker(on=frozenset({'warning', 'failed'}))
        assert t.observe('m', 'warning', 0).kind == PROBLEM
        assert t.observe('m', 'failed', 1) == Alert(PROBLEM, 'failed', 0)
        assert t.observe('m', 'warning', 2) is None

    def test_recovery_can_be_turned_off(self):
        t = self._tracker(recovery=False)
        t.observe('m', 'failed', 0)
        assert t.observe('m', 'online', 1) is None

    def test_a_seeded_problem_is_not_announced_again(self):
        t = self._tracker()
        t.seed('m', 'failed', since=5, now=100)
        assert t.observe('m', 'failed', 110) is None
        assert t.observe('m', 'online', 120) == Alert(RECOVERED, 'online', 5)

    def test_monitors_without_a_rule_are_ignored(self):
        assert self._tracker().observe('other', 'failed', 0) is None


class RecordingChannel:
    def __init__(self, channel_id='desk', fail=0):
        self.id = channel_id
        self.sent = []
        self._fail = fail

    async def send(self, message):
        if self._fail:
            self._fail -= 1
            raise ConnectionError("agent is not connected")
        self.sent.append(message)


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


class TestEngine:
    @pytest.fixture
    def engine(self, db_manager, monkeypatch):
        monkeypatch.setattr('vigil.core.notifications.RETRY_DELAYS', (0, 0))
        eng = NotificationEngine(db_manager, {'base_url': 'https://vigil.lan/'}, AgentRegistry())
        eng.channels = {'desk': RecordingChannel()}
        yield eng
        eng.stop()

    async def test_status_writes_notify_through_the_change_bus(self, engine, db_manager):
        engine.start([_plugin('nas')])
        db_manager.write_event('host.lan', 'nas', 'Nas', 'Pool tank is DEGRADED', level='ERROR')
        db_manager.insert_status('nas', 'failed')
        await _drain()

        [message] = engine.channels['desk'].sent
        assert message.title == 'Nas is failed'
        assert message.body == 'Pool tank is DEGRADED\nHost: host.lan'
        assert message.url == 'https://vigil.lan/monitor/nas'
        assert (message.monitor_name, message.host) == ('Nas', 'host.lan')
        assert message.timestamp > 0
        assert message.key == 'failed'

        db_manager.insert_status('nas', 'online')
        await _drain()
        assert engine.channels['desk'].sent[-1].title == 'Nas recovered'
        assert engine.channels['desk'].sent[-1].key == 'recovered'

    async def test_a_status_held_before_restart_is_not_announced(self, engine, db_manager):
        db_manager.insert_status('nas', 'failed')
        engine.start([_plugin('nas')])
        db_manager.insert_status('nas', 'failed')
        await _drain()
        assert engine.channels['desk'].sent == []

    async def test_a_failed_delivery_is_retried(self, engine, db_manager):
        engine.channels['desk'] = RecordingChannel(fail=2)
        engine.start([_plugin('nas')])
        db_manager.insert_status('nas', 'failed')
        await _drain()
        assert len(engine.channels['desk'].sent) == 1

    async def test_a_delivery_that_keeps_failing_is_reported_as_an_event(self, engine, db_manager):
        engine.channels['desk'] = RecordingChannel(fail=99)
        engine.start([_plugin('nas')])
        db_manager.insert_status('nas', 'failed')
        await _drain()
        messages = [e.message for e in db_manager.store.plugin_events(plugin_id='nas')]
        assert any("Could not notify 'desk'" in m for m in messages)

    async def test_without_channels_nothing_is_watched(self, db_manager):
        eng = NotificationEngine(db_manager, {}, AgentRegistry())
        eng.start([_plugin('nas')])
        assert eng._unsubscribe is None

    def test_monitor_url_escapes_the_id(self):
        assert monitor_url('http://v:8080', 'a b/c') == 'http://v:8080/monitor/a%20b%2Fc'
        assert monitor_url(None, 'a') is None


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, raw):
        self.sent.append(proto.decode(raw))


def _registry(caps=('notify',)):
    registry = AgentRegistry()
    registry.configure([{'id': 'desktop', 'token': 't'}])
    socket = FakeSocket()
    registry.get('desktop').attach(socket, {'caps': list(caps)})
    return registry, socket


class TestDesktopChannel:
    async def test_sends_a_notify_frame_to_the_agent(self):
        registry, socket = _registry()
        channel = DesktopChannel('desk', {'agent': 'desktop'}, registry)
        await channel.send(Message('Nas is failed', 'why', 'failed', PROBLEM, 'nas', 'https://v/monitor/nas'))
        assert socket.sent == [{'t': 'notify', 'title': 'Nas is failed', 'body': 'why',
                                'urgency': 'critical', 'url': 'https://v/monitor/nas',
                                'icon': 'vigil'}]

    async def test_icon_and_urgency_can_be_set_once_or_per_status(self):
        registry, socket = _registry()
        channel = DesktopChannel('desk', {'agent': 'desktop', 'icon': 'dialog-warning',
                                          'urgency': {'failed': 'normal'}}, registry)
        await channel.send(Message('t', 'b', 'failed', PROBLEM))
        await channel.send(Message('t', 'b', 'online', RECOVERED))
        assert [(f['icon'], f['urgency']) for f in socket.sent] == [
            ('dialog-warning', 'normal'), ('dialog-warning', 'low')]

    def test_bad_icon_or_urgency_settings_are_rejected(self):
        registry, _ = _registry()
        with pytest.raises(ValueError, match='low, normal or critical'):
            DesktopChannel('desk', {'agent': 'desktop', 'urgency': 'urgent'}, registry)
        with pytest.raises(ValueError, match='keys must be'):
            DesktopChannel('desk', {'agent': 'desktop', 'icon': {'down': 'x'}}, registry)

    async def test_an_agent_without_notify_support_fails_the_send(self):
        registry, _ = _registry(caps=('journal',))
        with pytest.raises(RuntimeError, match='does not support'):
            await DesktopChannel('desk', {'agent': 'desktop'}, registry).send(
                Message('t', 'b', 'failed', PROBLEM))

    async def test_a_disconnected_agent_fails_the_send(self):
        conn = AgentConnection('desktop', 'desktop')
        with pytest.raises(ConnectionError):
            await conn.notify('t', 'b')

    def test_bad_entries_are_skipped(self):
        registry, _ = _registry()
        channels = build_channels([
            {'id': 'desk', 'type': 'desktop', 'agent': 'desktop'},
            {'id': 'nowhere', 'type': 'desktop', 'agent': 'missing'},
            {'id': 'mystery', 'type': 'carrier-pigeon'},
            {'id': 'desk', 'type': 'desktop', 'agent': 'desktop'},
        ], registry)
        assert list(channels) == ['desk']


class TestTestEndpoint:
    def _client(self, send_test):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from vigil.core.ui.api import register_api

        notifications = SimpleNamespace(channels={'desk': object()}, send_test=send_test)
        engine = SimpleNamespace(plugins=[], db=SimpleNamespace(), notifications=notifications)
        app = FastAPI()
        register_api(app, engine)
        return TestClient(app)

    def test_sends_through_the_named_channel(self):
        sent = []

        async def send_test(channel_id):
            sent.append(channel_id)

        response = self._client(send_test).post('/api/notifications/desk/test')
        assert (response.status_code, response.json(), sent) == (200, {'sent': True}, ['desk'])

    def test_a_failed_delivery_is_reported(self):
        async def send_test(channel_id):
            raise ConnectionError('agent is not connected')

        response = self._client(send_test).post('/api/notifications/desk/test')
        assert response.status_code == 502
        assert response.json() == {'sent': False, 'error': 'agent is not connected'}

    def test_an_unknown_channel_is_not_found(self):
        async def send_test(channel_id):
            raise AssertionError('should not send')

        assert self._client(send_test).post('/api/notifications/nope/test').status_code == 404
