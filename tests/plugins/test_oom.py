import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.oom import Oom
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric

CFG = {"name": "test-oom", "id": "test-oom", "ssh_config": {"host": "test.host"}}

def _vmstat(oom_kill=0, include=True):
    lines = ["pgfault 1000", "pgmajfault 20"]
    if include:
        lines.append(f"oom_kill {oom_kill}")
    return "\n".join(lines) + "\n"



def _latest_status() -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == "test-oom"
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str) -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == "test-oom") & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(Oom, CFG)


class TestCollection:
    async def test_new_kill_fails_after_baseline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _vmstat(5), ""))
        assert _latest_status() == "online"
        run_cycle(plugin, lambda c: CmdResult(0, _vmstat(6), ""))
        assert _latest_status() == "failed"
        assert _latest_metric("oom_kills_new") == pytest.approx(1.0)

    async def test_kill_as_warning_when_configured(self, make_plugin, run_cycle):
        p = make_plugin(Oom, dict(CFG, is_warning=True))
        run_cycle(p, lambda c: CmdResult(0, _vmstat(0), ""))
        run_cycle(p, lambda c: CmdResult(0, _vmstat(1), ""))
        assert _latest_status() == "warning"

    async def test_alert_decays_after_alert_for_cycles(self, make_plugin, run_cycle):
        p = make_plugin(Oom, dict(CFG, alert_for=2))
        run_cycle(p, lambda c: CmdResult(0, _vmstat(0), ""))
        run_cycle(p, lambda c: CmdResult(0, _vmstat(1), ""))
        assert _latest_status() == "failed"
        run_cycle(p, lambda c: CmdResult(0, _vmstat(1), ""))
        assert _latest_status() == "warning"
        run_cycle(p, lambda c: CmdResult(0, _vmstat(1), ""))
        assert _latest_status() == "online"

    async def test_missing_counter_is_offline(self, plugin, run_cycle):
        run_cycle(plugin, lambda c: CmdResult(0, _vmstat(include=False), ""))
        assert _latest_status() == "offline"


class TestPushedEvents:
    def test_it_subscribes_to_the_kernel_journal(self, plugin):
        assert [s.kind for s in plugin.subscriptions()] == ['journal']

    def test_pushed_event_reports_a_kill(self, plugin):
        result = plugin.parse_event(plugin.id, {'message': 'Out of memory: Killed process 1 (x)'}, 0.0)
        assert result.status == 'failed'
        assert 'OOM killer fired' in result.logs[0][0]

    def test_empty_event_ignored(self, plugin):
        assert plugin.parse_event(plugin.id, {'message': '  '}, 0.0) is None
