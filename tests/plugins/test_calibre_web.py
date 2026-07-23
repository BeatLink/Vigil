from unittest.mock import AsyncMock

import pytest

from vigil.plugins.calibre_web import CalibreWebPlugin, _SEP, _looks_like_opds, _parse_response
from vigil.core.data.database import db, StatusHistory, Metric


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
    return make_plugin(CalibreWebPlugin, BASE_CFG)


def _respond(plugin, status=200, body=_OPDS_BODY):
    plugin.ssh_collector.fetch_output = AsyncMock(
        return_value=(0, f"{body}\n{_SEP}{status}", ""))


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
        body, status = _parse_response(f"{_OPDS_BODY}\n{_SEP}200")
        assert status == 200
        assert "feed" in body

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError):
            _parse_response("no separator")


class TestCalibreWebCollection:
    async def test_valid_feed_sets_online(self, plugin):
        _respond(plugin, status=200, body=_OPDS_BODY)
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_login_page_with_200_sets_failed(self, plugin):
        _respond(plugin, status=200, body="<html><body>Please log in</body></html>")
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_401_sets_failed(self, plugin):
        _respond(plugin, status=401, body="")
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "connection refused"))
        await plugin.on_collect()
        assert _latest_status() == "failed"


class TestCalibreWebActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
