from unittest.mock import AsyncMock

import pytest

from vigil.plugins.radicale import RadicalePlugin, _SEP, _build_probe_script, _parse_response
from vigil.core.data.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-radicale",
    "id":   "test-radicale",
    "username": "vigil",
    "password": "hunter2",
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(RadicalePlugin, BASE_CFG)


def _respond(plugin, status=207, body="<multistatus/>"):
    plugin.ssh_collector.fetch_output = AsyncMock(
        return_value=(0, f"{body}\n{_SEP}{status}", ""))


def _latest_status(plugin_id: str = "test-radicale") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-radicale") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBuildProbeScript:
    def test_uses_propfind(self):
        script = _build_probe_script("http://127.0.0.1:5232", 10, "vigil", None, "hunter2")
        assert "PROPFIND" in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_probe_script(
            "http://127.0.0.1:5232", 10, "vigil", "cat /run/secrets/radicale_vigil_password", None)
        assert "cat /run/secrets/radicale_vigil_password" in script


class TestParseResponse:
    def test_splits_body_and_status(self):
        body, status = _parse_response(f"<multistatus/>\n{_SEP}207")
        assert status == 207
        assert "<multistatus/>" in body

    def test_missing_separator_raises(self):
        with pytest.raises(ValueError, match="unexpected response"):
            _parse_response("no separator here")


class TestRadicaleCollection:
    async def test_207_sets_online(self, plugin):
        _respond(plugin, status=207)
        await plugin.on_collect()
        assert _latest_status() == "online"

    async def test_401_sets_failed(self, plugin):
        _respond(plugin, status=401)
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_500_sets_failed(self, plugin):
        _respond(plugin, status=500)
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin):
        plugin.ssh_collector.fetch_output = AsyncMock(return_value=(1, "", "connection refused"))
        await plugin.on_collect()
        assert _latest_status() == "failed"

    async def test_ok_records_metric(self, plugin):
        _respond(plugin, status=207)
        await plugin.on_collect()
        assert _latest_metric("propfind_ok") == 1.0


class TestRadicaleActions:
    async def test_on_action_always_returns_false(self, plugin):
        assert await plugin.on_action("anything") is False
