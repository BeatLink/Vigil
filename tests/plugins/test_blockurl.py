import json
from unittest.mock import AsyncMock

import pytest

from vigil.plugins.blockurl import BlockurlPlugin, _parse_response
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-blockurl",
    "id":   "test-blockurl",
    "api_key": "testkey",
    "min_domains": 1,
    "ssh_config": {"host": "test.host"},
}

_DOMAINS = [["example.com", 5], ["other.com", 3]]


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(BlockurlPlugin, BASE_CFG)


def _respond(plugin, domains=None):
    plugin.ssh_collector.fetch_output = AsyncMock(
        return_value=(0, json.dumps(domains if domains is not None else _DOMAINS), ""))


def _latest_status(plugin_id: str = "test-blockurl") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-blockurl") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestParseResponse:
    def test_parses_domain_list(self):
        data = _parse_response(json.dumps(_DOMAINS))
        assert len(data) == 2

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="not a list"):
            _parse_response('{"foo": "bar"}')

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            _parse_response("not json")


class TestBlockurlCollection:
    async def test_populated_list_sets_online(self, plugin):
        _respond(plugin)
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_empty_list_sets_warning(self, plugin):
        _respond(plugin, domains=[])
        await plugin.on_collect()
        assert _latest_status() == "warning"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "connection refused"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_records_url_total(self, plugin):
        _respond(plugin)
        await plugin.on_collect()
        assert _latest_metric("urls_total") == 8.0

    async def test_records_domain_total(self, plugin):
        _respond(plugin)
        await plugin.on_collect()
        assert _latest_metric("domains_total") == 2.0


class TestBlockurlActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
