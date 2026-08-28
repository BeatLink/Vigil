import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.connections import Connections
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-connections", "id": "test-connections", "ssh_config": {"host": "test.host"}}

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _make_tcp(states: list) -> str:
    lines = [TCP_HEADER]
    for i, st in enumerate(states):
        lines.append(f"   {i}: 0100007F:0050 00000000:0000 {st} 00000000:00000000 00:00000000 00000000     0        0 0\n")
    return "".join(lines)



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-connections"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-connections") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Connections, CFG)


from vigil.plugins.connections import _parse_states


class TestParseStates:
    def test_counts_established_and_listen(self):
        counts = _parse_states(_make_tcp(["01", "01", "0A"]))
        assert counts["ESTABLISHED"] == 2 and counts["LISTEN"] == 1

    def test_ignores_header(self):
        assert sum(_parse_states(_make_tcp(["01"])).values()) == 1

    def test_handles_lowercase_hex(self):
        assert _parse_states(_make_tcp(["0a"]))["LISTEN"] == 1

    def test_unknown_state_ignored(self):
        assert sum(_parse_states(_make_tcp(["FF"])).values()) == 0


class TestCollection:
    async def test_totals_are_recorded(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_tcp(["01", "0A", "06"]), ""))
        assert _latest_status() == "online"
        assert _latest_metric("conn_total") == pytest.approx(3.0)

    async def test_warning_on_high_total(self, make_plugin, run_cycle):
        p = make_plugin(Connections, dict(CFG, warning=2, threshold=10))
        run_cycle(p, lambda c: CmdResult(0, _make_tcp(["01", "01", "01"]), ""))
        assert _latest_status() == "warning"

    async def test_failed_on_flood(self, make_plugin, run_cycle):
        p = make_plugin(Connections, dict(CFG, warning=2, threshold=3))
        run_cycle(p, lambda c: CmdResult(0, _make_tcp(["01", "01", "01", "01"]), ""))
        assert _latest_status() == "failed"

    async def test_zero_connections_records_zero(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _make_tcp([]), ""))
        assert _latest_metric("conn_total") == pytest.approx(0.0)
        assert _latest_status() == "online"
