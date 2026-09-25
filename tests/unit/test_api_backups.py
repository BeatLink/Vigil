"""The /api/backups endpoint: one row per backup monitor, stalest first."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vigil.core.ui.api import register_api


def _backup(plugin_id, age):
    summary = {'repo': '/r', 'newest_archive_age': age, 'fresh': age is not None and age < 100}
    return SimpleNamespace(id=plugin_id, name=plugin_id, config={'type': 'borg'}, children=[],
                           target=None, backup_summary=lambda: summary)


def _client(plugins, statuses):
    db = SimpleNamespace(latest_statuses=lambda: statuses, latest_metrics=lambda: [],
                         recent_events=lambda **kw: [])
    app = FastAPI()
    register_api(app, SimpleNamespace(plugins=plugins, db=db))
    return TestClient(app)


def test_only_backup_monitors_are_listed_stalest_first():
    other = SimpleNamespace(id='cpu', name='cpu', config={'type': 'cpu'}, children=[], target=None)
    plugins = [_backup('fresh', 10), other, _backup('never', None), _backup('old', 500)]
    body = _client(plugins, {'fresh': 'online', 'old': 'failed'}).get('/api/backups').json()
    assert [r['id'] for r in body['repositories']] == ['never', 'old', 'fresh']
    assert body['total'] == 3 and body['healthy'] == 1 and body['unhealthy'] == 2
