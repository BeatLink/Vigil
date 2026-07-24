import json

import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.openbooks import (
    Openbooks,
    _build_probe_script,
    _parse_response,
    _MSG_TYPE_CONNECT,
    _MSG_TYPE_STATUS,
    _APPEARANCE_SUCCESS,
    _APPEARANCE_DANGER,
)
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-openbooks",
    "id":   "test-openbooks",
    "ssh_config": {"host": "test.host"},
}


def _connect_ok():
    return json.dumps({"type": _MSG_TYPE_CONNECT, "payload": {"appearance": _APPEARANCE_SUCCESS}})


def _connect_fail():
    return json.dumps({"type": _MSG_TYPE_STATUS, "payload": {"appearance": _APPEARANCE_DANGER}})


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Openbooks, BASE_CFG)


def _latest_status(plugin_id: str = "test-openbooks") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-openbooks") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestBuildProbeScript:
    def test_uses_websocat(self):
        script = _build_probe_script("ws://127.0.0.1:9777/ws", 8)
        assert "websocat" in script

    def test_sends_connect_message(self):
        script = _build_probe_script("ws://127.0.0.1:9777/ws", 8)
        assert '"type": 1' in script


class TestParseResponse:
    def test_parses_connect_success(self):
        msg_type, appearance = _parse_response(_connect_ok())
        assert msg_type == _MSG_TYPE_CONNECT
        assert appearance == _APPEARANCE_SUCCESS

    def test_no_parseable_message_raises(self):
        with pytest.raises(ValueError):
            _parse_response("garbage, not json")


class TestOpenbooksCollection:
    async def test_connect_success_sets_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _connect_ok(), ""))
        assert _latest_status() == "online"

    async def test_connect_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _connect_fail(), ""))
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(1, "", "timed out"))
        assert _latest_status() == "failed"

    async def test_garbage_response_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "not json at all", ""))
        assert _latest_status() == "failed"

    async def test_records_bridge_connected_metric(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _connect_ok(), ""))
        assert _latest_metric("bridge_connected") == 1.0


class TestOpenbooksActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
