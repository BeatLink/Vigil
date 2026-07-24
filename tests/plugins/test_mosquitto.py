import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.mosquitto import (
    MosquittoCollectorPlugin,
    _TIMED_OUT,
    _MISMATCH,
    _auth_flags,
    _build_probe_script,
)
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-mosquitto",
    "id":   "test-mosquitto",
    "host": "127.0.0.1",
    "port": 1883,
    "username": "vigil",
    "password": "hunter2",
    "probe_topic": "vigil/probe/test-mosquitto",
    "probe_timeout": 5,
    "ssh_config": {"host": "test.host"},
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(MosquittoCollectorPlugin, BASE_CFG)


def _ok(nonce="vigil-probe-abc123"):
    return CmdResult(0, nonce, "")


def _failure(stderr):
    return CmdResult(1, "", stderr)


def _latest_status(plugin_id: str = "test-mosquitto") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-mosquitto") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestAuthFlags:
    def test_no_username_means_no_auth(self):
        assert _auth_flags(None, None, None) == ''

    def test_username_with_password(self):
        flags = _auth_flags("vigil", None, "hunter2")
        assert "-u vigil" in flags
        assert "-P hunter2" in flags

    def test_username_with_password_command_uses_shell_var(self):
        flags = _auth_flags("vigil", "cat /run/secrets/mosquitto_vigil", None)
        assert "-u vigil" in flags
        assert '-P "$__pw"' in flags

    def test_username_only(self):
        flags = _auth_flags("vigil", None, None)
        assert flags == "-u vigil"


class TestBuildProbeScript:
    def test_includes_topic(self):
        script = _build_probe_script(
            "127.0.0.1", 1883, "vigil/probe/x", 5, "vigil", None, "hunter2")
        assert "vigil/probe/x" in script

    def test_password_command_runs_on_remote_host(self):
        script = _build_probe_script(
            "127.0.0.1", 1883, "vigil/probe/x", 5, "vigil",
            "cat /run/secrets/mosquitto_vigil", None)
        assert "cat /run/secrets/mosquitto_vigil" in script

    def test_uses_pub_and_sub(self):
        script = _build_probe_script(
            "127.0.0.1", 1883, "vigil/probe/x", 5, None, None, None)
        assert "mosquitto_pub" in script
        assert "mosquitto_sub" in script


class TestMosquittoCollection:
    async def test_successful_roundtrip_sets_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _ok())
        assert _latest_status() == "online"

    async def test_successful_roundtrip_records_metrics(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _ok())
        assert _latest_metric("roundtrip_ok") == 1.0
        assert _latest_metric("roundtrip_ms") is not None

    async def test_timeout_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _failure(_TIMED_OUT))
        assert _latest_status() == "failed"

    async def test_timeout_records_roundtrip_ok_as_zero(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _failure(_TIMED_OUT))
        assert _latest_metric("roundtrip_ok") == 0.0

    async def test_mismatch_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _failure(f"{_MISMATCH}: expected a, got b"))
        assert _latest_status() == "failed"

    async def test_ssh_failure_sets_failed(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: _failure("connection refused"))
        assert _latest_status() == "failed"


class TestMosquittoActions:
    async def test_on_action_always_returns_none(self, plugin):
        assert plugin.plan_action("anything") is None
