import pytest

from vigil.plugins.calibre_web import CalibreWeb, _SEP, _looks_like_opds, _parse_response
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-calibre-web",
    "id":   "test-calibre-web",
    "username": "vigil",
    "password": "hunter2",
    "ssh_config": {"host": "test.host"},
}

_OPDS_BODY = '<feed xmlns="http://www.w3.org/2005/Atom"><title>Calibre-Web</title></feed>'


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(CalibreWeb, BASE_CFG)


def _result(status=200, body=_OPDS_BODY, time_total="0.05"):
    return CmdResult(0, f"{body}\n{_SEP}{status} {time_total}", "")


def _latest_status(plugin_id: str = "test-calibre-web") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


class TestLooksLikeOpds:
    def test_real_feed(self):
        assert _looks_like_opds(_OPDS_BODY) is True

    def test_html_page(self):
        assert _looks_like_opds("<html><body>Login</body></html>") is False


class TestParseResponse:
    def test_splits_body_and_status(self):
        body, status, elapsed_ms = _parse_response(f"{_OPDS_BODY}\n{_SEP}200 0.05")
        assert status == 200
        assert "feed" in body
        assert elapsed_ms == pytest.approx(50.0)

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError):
            _parse_response("no separator")


class TestCalibreWebCollection:
    async def test_valid_feed_sets_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(status=200, body=_OPDS_BODY))
        assert _latest_status() == "online"

    async def test_login_page_with_200_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(status=200, body="<html><body>Please log in</body></html>"))
        assert _latest_status() == "failed"

    async def test_401_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _result(status=401, body=""))
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "connection refused"))
        assert _latest_status() == "failed"


class TestCalibreWebActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
