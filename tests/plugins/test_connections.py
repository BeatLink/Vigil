import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.connections import ConnectionsCollectorPlugin, _parse_states
from vigil.core.connectors.orchestration.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-conn",
    "id":   "test-conn",
    "ssh_config": {"host": "test.host"},
}

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _make_tcp(states: list) -> str:
    lines = [TCP_HEADER]
    for i, st in enumerate(states):
        lines.append(f"   {i}: 0100007F:0050 00000000:0000 {st} 00000000:00000000 00:00000000 00000000     0        0 0\n")
    return "".join(lines)


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(ConnectionsCollectorPlugin, BASE_CFG)


def _latest_status(plugin_id: str = "test-conn"):
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


class TestParseStates:
    def test_counts_established(self):
        block = _make_tcp(["01", "01", "0A"])
        counts = _parse_states(block)
        assert counts["ESTABLISHED"] == 2
        assert counts["LISTEN"] == 1

    def test_ignores_header(self):
        block = _make_tcp(["01"])
        counts = _parse_states(block)
        assert sum(counts.values()) == 1

    def test_handles_lowercase_hex(self):
        block = _make_tcp(["0a"])
        counts = _parse_states(block)
        assert counts["LISTEN"] == 1

    def test_unknown_state_ignored(self):
        block = _make_tcp(["FF"])
        counts = _parse_states(block)
        assert sum(counts.values()) == 0


class TestConnectionsCollection:
    async def test_normal_online(self, plugin, run_cycle):
        stdout = _make_tcp(["01", "0A", "06"])
        run_cycle(plugin, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "online"
        assert _latest_metric("test-conn", "total") == pytest.approx(3.0)
        assert _latest_metric("test-conn", "established") == pytest.approx(1.0)
        assert _latest_metric("test-conn", "listen") == pytest.approx(1.0)
        assert _latest_metric("test-conn", "time_wait") == pytest.approx(1.0)

    async def test_warning_on_high_total(self, make_plugin, run_cycle):
        p = make_plugin(ConnectionsCollectorPlugin, {**BASE_CFG, "total_warning": 2, "total_threshold": 10})
        stdout = _make_tcp(["01", "01", "01"])
        run_cycle(p, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "warning"

    async def test_failed_on_flood(self, make_plugin, run_cycle):
        p = make_plugin(ConnectionsCollectorPlugin, {**BASE_CFG, "total_warning": 2, "total_threshold": 3})
        stdout = _make_tcp(["01", "01", "01", "01"])
        run_cycle(p, lambda c: CmdResult(0, stdout, ""))
        assert _latest_status() == "failed"

    async def test_zero_connections_records_zero(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, TCP_HEADER, ""))
        assert _latest_metric("test-conn", "total") == pytest.approx(0.0)
        assert _latest_status() == "online"

    async def test_ssh_failure_fails(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(-1, "", "err"))
        assert _latest_status() == "failed"


class TestConnectionsActions:
    async def test_on_action_returns_false(self, plugin):
        assert plugin.plan_action("anything") is None
