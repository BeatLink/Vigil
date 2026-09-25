"""The /api/summary endpoint: counts of leaf monitors by status, for callers
that can display a number but cannot aggregate a list of monitors themselves."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigil.core.ui.api import register_api


def _plugin(plugin_id, children=()):
    return SimpleNamespace(id=plugin_id, name=plugin_id, config={'type': 'dummy'},
                           children=list(children), target=None)


def _engine(plugins, statuses):
    db = SimpleNamespace(latest_statuses=lambda: statuses, latest_metrics=lambda: [],
                         recent_events=lambda **kw: [])
    return SimpleNamespace(plugins=plugins, db=db)


@pytest.fixture
def client():
    def _build(plugins, statuses):
        app = FastAPI()
        register_api(app, _engine(plugins, statuses))
        return TestClient(app)
    return _build


class TestSummaryCounts:
    def test_each_status_is_counted(self, client):
        plugins = [_plugin('a'), _plugin('b'), _plugin('c'), _plugin('d')]
        statuses = {'a': 'online', 'b': 'warning', 'c': 'failed', 'd': 'offline'}
        body = client(plugins, statuses).get('/api/summary').json()
        assert body['total'] == 4
        assert (body['online'], body['warning'], body['failed'], body['offline']) == (1, 1, 1, 1)

    def test_a_monitor_with_no_status_yet_is_unavailable(self, client):
        body = client([_plugin('a')], {}).get('/api/summary').json()
        assert body['unavailable'] == 1
        assert body['total'] == 1

    def test_groups_are_not_counted(self, client):
        group = _plugin('group', children=[_plugin('leaf')])
        body = client([group], {'group': 'online', 'leaf': 'online'}).get('/api/summary').json()
        assert body['total'] == 1

    def test_unhealthy_is_everything_not_online(self, client):
        plugins = [_plugin('a'), _plugin('b'), _plugin('c')]
        statuses = {'a': 'online', 'b': 'failed', 'c': 'warning'}
        body = client(plugins, statuses).get('/api/summary').json()
        assert body['unhealthy'] == 2

    def test_an_unknown_status_still_counts_toward_the_total(self, client):
        body = client([_plugin('a')], {'a': 'weird'}).get('/api/summary').json()
        assert body['total'] == 1
        assert body['weird'] == 1
