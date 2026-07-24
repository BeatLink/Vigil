import pytest
from unittest.mock import AsyncMock

pytestmark = pytest.mark.asyncio
from vigil.plugins.ports import PortsCollectorPlugin, _parse_results, _safe_metric_name, _build_probe_script
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-ports",
    "id":   "test-ports",
    "ssh_config": {"host": "test.host"},
    "checks": [
        {"name": "SSH", "host": "10.0.0.1", "port": 22},
        {"name": "API", "url": "https://api.example.com/health"},
    ],
}


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(PortsCollectorPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-ports"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(name: str, metric: str):
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


class TestHelpers:
    def test_safe_metric_name(self):
        assert _safe_metric_name("My Web!") == "my_web"
        assert _safe_metric_name("https://a.b/c") == "https___a_b_c"

    def test_parse_results_ok_and_fail(self):
        results = _parse_results("0 12\n1 FAIL\n", 2)
        assert results[0] == pytest.approx(12.0)
        assert results[1] is None

    def test_parse_results_missing_index_stays_none(self):
        results = _parse_results("0 5\n", 2)
        assert results[0] == pytest.approx(5.0)
        assert results[1] is None

    def test_build_script_has_tcp_and_url(self):
        script = _build_probe_script(BASE_CFG["checks"], 5)
        assert "/dev/tcp/10.0.0.1/22" in script
        assert "curl" in script


class TestPortsCollection:
    async def test_all_reachable_online(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "0 3\n1 45\n", ""))
        assert _latest_status() == "online"
        assert _latest_metric("test-ports", "ssh_up") == pytest.approx(1.0)
        assert _latest_metric("test-ports", "ssh_latency_ms") == pytest.approx(3.0)
        assert _latest_metric("test-ports", "api_latency_ms") == pytest.approx(45.0)

    async def test_one_down_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, "0 3\n1 FAIL\n", ""))
        assert _latest_status() == "failed"
        assert _latest_metric("test-ports", "api_up") == pytest.approx(0.0)

    async def test_no_checks_offline(self, make_plugin, run_cycle):
        p = make_plugin(PortsCollectorPlugin, {**BASE_CFG, "checks": []})
        run_cycle(p)
        assert _latest_status() == "offline"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "err"))
        assert _latest_status() == "failed"

    async def test_auto_labels_unnamed_check(self, make_plugin):
        p = make_plugin(PortsCollectorPlugin, {
            **BASE_CFG,
            "checks": [{"host": "1.2.3.4", "port": 80}],
        })
        assert p.checks[0]["name"] == "1.2.3.4:80"
        assert p.checks[0]["metric"] == "1_2_3_4_80"


class TestPortsActions:
    async def test_on_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
