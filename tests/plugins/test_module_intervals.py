import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.disks import Disks
from vigil.plugins.base.module_plugin import Module, ModularPlugin
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-intervals",
    "id":   "test-intervals",
    "interval": 60,
    "ssh_config": {"host": "test.host"},
}


class _Clock:
    """A monotonic clock the test advances by hand, standing in for time.monotonic."""

    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds: float):
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def make_timed(make_plugin, clock):
    def factory(modules):
        plugin = make_plugin(Disks, dict(BASE_CFG, modules=modules))
        plugin._now = lambda: clock.now
        return plugin
    return factory


_DISKSTATS = (
    "   8       0 sda 100 0 0 50 200 0 0 80 0 0 0 0\n"
    "---SNAP---\n"
    "   8       0 sda 100 0 8 50 200 0 16 80 0 0 0 0\n")


def _collect(plugin, run_cycle, *, smart="PASS /dev/sda\n"):
    """Run one cycle, answering each command as it is issued so the schedule
    advances exactly once."""
    def respond(command):
        body = smart if 'smartctl' in command.text else _DISKSTATS
        return CmdResult(0, body, "")
    return run_cycle(plugin, respond)


def _latest_status(plugin_id: str = "test-intervals"):
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _metric_count(metric: str, name: str = "test-intervals") -> int:
    with db.connection_context():
        return Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).count()


class TestDueness:
    def test_a_module_without_an_interval_runs_every_cycle(self, make_timed, clock):
        p = make_timed(['smart'])
        assert len(p.commands()) == 1
        clock.advance(60)
        assert len(p.commands()) == 1

    def test_a_module_collects_on_its_first_cycle(self, make_timed):
        p = make_timed({'smart': {'interval': 3600}})
        assert len(p.commands()) == 1

    def test_a_module_sits_out_cycles_inside_its_interval(self, make_timed, clock):
        p = make_timed({'smart': {'interval': 3600}})
        p.commands()
        for _ in range(59):
            clock.advance(60)
            assert p.commands() == []

    def test_a_module_collects_again_once_its_interval_elapses(self, make_timed, clock):
        p = make_timed({'smart': {'interval': 3600}})
        p.commands()
        clock.advance(3600)
        assert len(p.commands()) == 1

    def test_an_interval_accepts_a_duration_string(self, make_timed):
        p = make_timed({'smart': {'interval': '1h'}})
        assert p._module('smart').interval == 3600

    def test_an_interval_below_the_plugins_runs_every_cycle(self, make_timed, clock):
        p = make_timed({'smart': {'interval': 5}})
        p.commands()
        clock.advance(60)
        assert len(p.commands()) == 1

    def test_modules_keep_independent_schedules(self, make_timed, clock):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        assert len(p.commands()) == 2
        clock.advance(60)
        assert [c for c in p.commands() if 'smartctl' in c.text] == []
        assert len(p.commands()) == 1


class TestResultSlicing:
    def test_a_skipped_module_does_not_shift_another_modules_results(
            self, make_timed, clock, run_cycle):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        _collect(p, run_cycle)
        clock.advance(60)
        _collect(p, run_cycle)
        assert _latest_status() == 'online'
        assert _metric_count('read_kbps') == 2

    def test_a_skipped_module_writes_no_metrics(self, make_timed, clock, run_cycle):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        _collect(p, run_cycle)
        clock.advance(60)
        _collect(p, run_cycle)
        assert _metric_count('disks_ok') == 1


class TestCarriedStatus:
    def test_a_skipped_modules_verdict_still_counts(self, make_timed, clock, run_cycle):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        _collect(p, run_cycle, smart="FAIL /dev/sda\n")
        assert _latest_status() == 'failed'
        clock.advance(60)
        _collect(p, run_cycle)
        assert _latest_status() == 'failed'

    def test_a_cleared_verdict_is_dropped_on_the_next_run(
            self, make_timed, clock, run_cycle):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        _collect(p, run_cycle, smart="FAIL /dev/sda\n")
        clock.advance(3600)
        _collect(p, run_cycle, smart="PASS /dev/sda\n")
        assert _latest_status() == 'online'

    def test_a_cycle_where_no_module_is_due_reports_the_held_statuses(
            self, make_timed, clock, run_cycle):
        p = make_timed({'smart': {'interval': 3600}})
        _collect(p, run_cycle, smart="FAIL /dev/sda\n")
        clock.advance(60)
        assert p.commands() == []
        _collect(p, run_cycle)
        assert _latest_status() == 'failed'


class TestUiIsUnaffected:
    def test_a_module_off_its_cycle_keeps_its_cards(self, make_timed, clock):
        p = make_timed({'smart': {'interval': 3600}, 'io': {}})
        p.commands()
        clock.advance(60)
        p.commands()
        assert 'smart_ok_card' in p.UI_SPEC['cards']


class TestBaseDefaults:
    def test_a_module_defaults_to_no_interval(self):
        module = Module(plugin=None, options={})
        assert module.interval is None
        assert module.due(now=0.0)

    def test_a_plugin_with_no_module_types_reports_it(self, make_plugin):
        class _Empty(ModularPlugin):
            MODULE_LABEL = 'empty'

        result = make_plugin(_Empty, BASE_CFG).parse([])
        assert result.status == 'offline'
