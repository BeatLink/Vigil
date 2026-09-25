"""Starting up with a database to load: the engine loads it in run(), off the
event loop, and nothing else writes to the store or serves a page until then."""

import pytest
import yaml
from unittest.mock import patch
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from vigil.__main__ import VigilEngine
from vigil.core.database.database import DatabaseManager, db
from vigil.core.ui.startup import StartingUpMiddleware


@pytest.fixture(autouse=True)
def close_db():
    yield
    if not db.is_closed():
        db.close()


@pytest.fixture
def saved(tmp_path):
    """A database holding one monitor's status from a previous run."""
    path = str(tmp_path / "vigil.db")
    manager = DatabaseManager(path)
    manager.insert_status("cpu", "failed")
    manager.flush()
    db.close()
    return path


def _engine(tmp_path, db_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"plugins": []}))
    with patch("vigil.core.connectors.engine.SSHConnection"):
        return VigilEngine(str(cfg), db_path_override=db_path)


class TestLoading:
    def test_constructing_the_engine_loads_nothing(self, tmp_path, saved):
        engine = _engine(tmp_path, saved)
        assert engine.ready is False
        assert engine.db.latest_statuses() == {}

    async def test_run_loads_the_saved_state_then_is_ready(self, tmp_path, saved):
        engine = _engine(tmp_path, saved)
        await engine.run()
        try:
            assert engine.ready is True
            assert engine.db.latest_statuses() == {"cpu": "failed"}
        finally:
            engine.shutdown()

    async def test_an_agent_that_connected_early_is_collected_once_ready(self, tmp_path, saved):
        engine = _engine(tmp_path, saved)
        seen = []
        engine._on_agent_connected('node-a')
        assert engine._early_agents == {'node-a'}
        original = engine._on_agent_connected
        engine._on_agent_connected = lambda agent_id: (seen.append((agent_id, engine.ready)), original(agent_id))
        await engine.run()
        try:
            assert seen == [('node-a', True)]
            assert engine._early_agents == set()
        finally:
            engine.shutdown()

    def test_an_event_before_ready_touches_nothing(self, tmp_path, saved):
        engine = _engine(tmp_path, saved)
        engine._event_targets = {'cpu:sample': object()}
        engine._on_agent_event('node-a', 'cpu:sample', 1.0, {'exit_code': 0, 'stdout': '', 'stderr': ''})
        assert engine.db.latest_statuses() == {}


class _Engine:
    ready = False


@pytest.fixture
def client():
    engine = _Engine()
    app = Starlette(routes=[Route('/{path:path}', lambda request: PlainTextResponse('served'))])
    app.add_middleware(StartingUpMiddleware, engine=engine)
    return TestClient(app), engine


class TestStartingPage:
    def test_a_page_asks_the_browser_to_come_back(self, client):
        http, _ = client
        response = http.get('/monitor/cpu')
        assert response.status_code == 503
        assert response.headers['retry-after'] == '5'
        assert 'Vigil is starting' in response.text and 'http-equiv="refresh"' in response.text

    @pytest.mark.parametrize('path', ['/api/summary', '/metrics'])
    def test_machines_get_json(self, client, path):
        http, _ = client
        response = http.get(path)
        assert response.status_code == 503
        assert response.json() == {'status': 'starting'}

    def test_the_page_icon_is_still_served(self, client):
        http, _ = client
        assert http.get('/icon.svg').text == 'served'

    def test_everything_is_served_once_ready(self, client):
        http, engine = client
        engine.ready = True
        assert http.get('/').text == 'served'
        assert http.get('/api/summary').text == 'served'
