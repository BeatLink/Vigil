import pytest

pytestmark = pytest.mark.asyncio
from vigil.plugins.system_stats import SystemStats, _module_options, _worst
from vigil.core.connectors.types import CmdResult
from vigil.core.database.database import db, StatusHistory, Metric


BASE_CFG = {
    "name": "test-system-stats",
    "id":   "test-system-stats",
    "ssh_config": {"host": "test.host"},
}

_MEM_TOTAL_KB = 16_000_000


def _proc_stat(idle_delta=500, total_delta=1000):
    """Two /proc/stat samples, the second `total_delta` jiffies later with
    `idle_delta` of them idle."""
    return (f"cpu  1000 0 0 1000 0 0 0 0\n"
            f"cpu  {1000 + total_delta - idle_delta} 0 0 {1000 + idle_delta} 0 0 0 0\n")


def _nvidia_smi(util=10.0, mem_used=1000, mem_total=8000, temp=50.0, count=1):
    return "".join(
        f"{idx}, {util}, {mem_used}, {mem_total}, {temp}\n" for idx in range(count))


def _meminfo(avail_kb):
    return f"MemTotal:       {_MEM_TOTAL_KB} kB\nMemAvailable:   {avail_kb} kB\n"


def _loadavg(one, five=0.5, fifteen=0.5, cpus=4):
    return f"LOAD:{one} {five} {fifteen} 1/500 12345\nCPUS:{cpus}\n"


def _sensors(*temps_mc):
    return "".join(f"SENSOR:x86_pkg_temp_{i}:{t}\n" for i, t in enumerate(temps_mc))


def _proc_stat_snaps(intr_delta=1000, ctxt_delta=2000):
    """The interrupts module's two-snapshot /proc/stat output."""
    def snap(intr, ctxt):
        return f"cpu  100 0 50 900 10 0 5 0\nintr {intr} 20 0 0\nctxt {ctxt}\n"
    return snap(1_000_000, 5_000_000) + "---SNAP---\n" + snap(
        1_000_000 + intr_delta, 5_000_000 + ctxt_delta)


def _vmstat(oom_kill=0, include=True):
    lines = ["pgfault 1000", "pgmajfault 20"]
    if include:
        lines.append(f"oom_kill {oom_kill}")
    return "\n".join(lines) + "\n"


def _outputs(plugin, *, avail_kb=8_000_000, load=1.0, cpus=4, oom_kill=0,
             oom_include=True, cpu_idle=500, gpu_util=10.0, gpu_temp=50.0,
             gpu_count=1, intr_delta=1000, temps_mc=(42_000, 45_000),
             codes=None, stderrs=None):
    """Map the plugin's concatenated commands to fake results, in the order
    the enabled modules declared them."""
    bodies = []
    for command in plugin.commands():
        if '---SNAP---' in command.text:
            bodies.append(_proc_stat_snaps(intr_delta=intr_delta))
        elif '/proc/stat' in command.text:
            bodies.append(_proc_stat(idle_delta=cpu_idle))
        elif 'meminfo' in command.text:
            bodies.append(_meminfo(avail_kb))
        elif 'loadavg' in command.text:
            bodies.append(_loadavg(load, cpus=cpus))
        elif 'thermal_zone' in command.text:
            bodies.append(_sensors(*temps_mc))
        elif 'nvidia-smi' in command.text:
            bodies.append(_nvidia_smi(util=gpu_util, temp=gpu_temp, count=gpu_count))
        else:
            bodies.append(_vmstat(oom_kill, oom_include))
    codes = codes or [0] * len(bodies)
    stderrs = stderrs or [""] * len(bodies)
    return [CmdResult(code, body, err)
            for code, body, err in zip(codes, bodies, stderrs)]


def _collect(plugin, run_cycle, **kwargs):
    results = iter(_outputs(plugin, **kwargs))
    return run_cycle(plugin, lambda c: next(results))


def _latest_status(plugin_id: str = "test-system-stats") -> str | None:
    with db.connection_context():
        row = StatusHistory.select().where(
            StatusHistory.collector_id == plugin_id
        ).order_by(StatusHistory.timestamp.desc()).first()
    return row.state if row else None


def _latest_metric(metric: str, name: str = "test-system-stats") -> float | None:
    with db.connection_context():
        row = Metric.select().where(
            (Metric.collector == name) & (Metric.metric_name == metric)
        ).order_by(Metric.timestamp.desc()).first()
    return row.value if row else None


ALL_MODULES = ['cpu', 'memory', 'load', 'temperature', 'interrupts', 'gpu', 'oom']


@pytest.fixture
def plugin(make_plugin):
    return make_plugin(SystemStats, dict(BASE_CFG, modules=ALL_MODULES))


class TestModuleSelection:
    def test_known_modules_are_in_canonical_order(self, plugin):
        assert [m.key for m in plugin.modules] == [
            'cpu', 'memory', 'load', 'temperature', 'interrupts', 'gpu', 'oom']

    def test_every_module_is_opt_in(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['gpu', 'cpu']))
        assert [m.key for m in p.modules] == ['cpu', 'gpu']
        assert len(p.commands()) == 2

    def test_an_absent_modules_block_enables_the_defaults(self, make_plugin):
        assert [m.key for m in make_plugin(SystemStats, BASE_CFG).modules] == [
            'cpu', 'memory', 'load', 'oom']

    def test_an_empty_modules_block_enables_nothing(self, make_plugin):
        assert make_plugin(SystemStats, dict(BASE_CFG, modules=[])).modules == []

    def test_list_form_selects_modules(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['oom', 'memory']))
        assert [m.key for m in p.modules] == ['memory', 'oom']

    def test_mapping_form_disables_module(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'memory': {'warning': 50, 'threshold': 60},
            'load': False,
            'oom': {'enabled': False},
        }))
        assert [m.key for m in p.modules] == ['memory']
        assert p.modules[0].warning == 50

    def test_bare_true_keeps_defaults(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={'load': True}))
        assert [m.key for m in p.modules] == ['load']
        assert p.modules[0].warning is None

    def test_unknown_module_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="unknown module"):
            make_plugin(SystemStats, dict(BASE_CFG, modules=['swap']))

    def test_malformed_modules_block_rejected(self, make_plugin):
        with pytest.raises(ValueError, match="must be a mapping or a list"):
            make_plugin(SystemStats, dict(BASE_CFG, modules="memory"))

    def test_module_options_of_an_absent_block_are_the_defaults(self):
        assert list(_module_options({})) == ['cpu', 'memory', 'load', 'oom']

    def test_module_options_of_an_empty_block_are_empty(self):
        assert _module_options({'modules': []}) == {}


class TestCollection:
    async def test_all_modules_report_metrics(self, plugin, run_cycle):
        p = plugin
        _collect(p, run_cycle, avail_kb=8_000_000, load=1.0, cpus=4, oom_kill=2, cpu_idle=750)
        assert _latest_status() == "online"
        assert _latest_metric("cpu_pct") == pytest.approx(25.0)
        assert _latest_metric("memory_pct") == pytest.approx(50.0)
        assert _latest_metric("load_pct_1m") == pytest.approx(25.0)
        assert _latest_metric("temp_c") == pytest.approx(45.0)
        assert _latest_metric("irq_per_sec") == pytest.approx(1000.0)
        assert _latest_metric("gpu_util") == pytest.approx(10.0)
        assert _latest_metric("oom_kills_total") == pytest.approx(2.0)

    async def test_disabled_module_collects_nothing(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['memory']))
        _collect(p, run_cycle, avail_kb=8_000_000)
        assert _latest_metric("memory_pct") == pytest.approx(50.0)
        assert _latest_metric("load_pct_1m") is None

    async def test_one_log_line_per_module(self, plugin, run_cycle):
        result = _collect(plugin, run_cycle)
        assert len(result.logs) == len(plugin.modules)

    async def test_worst_module_status_wins(self, plugin, run_cycle):
        _collect(plugin, run_cycle, avail_kb=400_000)  # 97.5% memory -> failed
        assert _latest_status() == "failed"

    async def test_memory_thresholds_configurable(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'memory': {'warning': 40, 'threshold': 90}}))
        _collect(p, run_cycle, avail_kb=8_000_000)  # 50% memory
        assert _latest_status() == "warning"

    async def test_load_without_thresholds_stays_online(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['load']))
        _collect(p, run_cycle, load=16.0, cpus=4)  # 400% of capacity
        assert _latest_status() == "online"
        assert _latest_metric("load_pct_1m") == pytest.approx(400.0)

    async def test_load_thresholds_configurable(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'load': {'warning': 100, 'threshold': 200}}))
        _collect(p, run_cycle, load=12.0, cpus=4)  # 300%
        assert _latest_status() == "failed"

    async def test_failed_command_isolated_to_its_module(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['memory', 'load']))
        _collect(p, run_cycle, codes=[1, 0])
        assert _latest_status() == "failed"
        assert _latest_metric("load_pct_1m") == pytest.approx(25.0)

    async def test_no_modules_enabled_reports_offline(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=[]))
        run_cycle(p)
        assert _latest_status() == "offline"


class TestOomModule:
    async def test_new_kill_fails_after_baseline(self, plugin, run_cycle):
        _collect(plugin, run_cycle, oom_kill=5)
        assert _latest_status() == "online"
        _collect(plugin, run_cycle, oom_kill=6)
        assert _latest_status() == "failed"
        assert _latest_metric("oom_kills_new") == pytest.approx(1.0)

    async def test_kill_as_warning_when_configured(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'oom': {'is_warning': True}}))
        _collect(p, run_cycle, oom_kill=0)
        _collect(p, run_cycle, oom_kill=1)
        assert _latest_status() == "warning"

    async def test_alert_decays_after_alert_for_cycles(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={'oom': {'alert_for': 2}}))
        _collect(p, run_cycle, oom_kill=0)
        _collect(p, run_cycle, oom_kill=1)
        assert _latest_status() == "failed"
        _collect(p, run_cycle, oom_kill=1)
        assert _latest_status() == "warning"
        _collect(p, run_cycle, oom_kill=1)
        assert _latest_status() == "online"

    async def test_missing_counter_is_offline(self, plugin, run_cycle):
        _collect(plugin, run_cycle, oom_include=False)
        assert _latest_status() == "offline"

    def test_journal_subscription_only_when_enabled(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['oom']))
        assert [s.kind for s in p.subscriptions()] == ['journal']
        assert make_plugin(SystemStats, dict(BASE_CFG, modules=['memory'])).subscriptions() == []

    def test_pushed_event_reports_a_kill(self, plugin):
        result = plugin.parse_event(plugin.id, {'message': 'Out of memory: Killed process 1 (x)'}, 0.0)
        assert result.status == 'failed'
        assert 'OOM killer fired' in result.logs[0][0]

    def test_empty_event_ignored(self, plugin):
        assert plugin.parse_event(plugin.id, {'message': '  '}, 0.0) is None


class TestCpuModule:
    async def test_busy_cpu_crosses_thresholds(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'cpu': {'warning': 70, 'threshold': 85}}))
        _collect(p, run_cycle, cpu_idle=100)  # 90% busy
        assert _latest_status() == "failed"
        assert _latest_metric("cpu_pct") == pytest.approx(90.0)

    async def test_idle_cpu_is_online(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['cpu']))
        _collect(p, run_cycle, cpu_idle=1000)
        assert _latest_status() == "online"
        assert _latest_metric("cpu_pct") == pytest.approx(0.0)


class TestTemperatureModule:
    async def test_hottest_zone_is_the_status_and_every_zone_is_kept(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['temperature']))
        _collect(p, run_cycle, temps_mc=(42_000, 55_000))
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") == pytest.approx(55.0)
        assert _latest_metric("temp_zone_x86_pkg_temp_1") == pytest.approx(55.0)

    async def test_thresholds_configurable(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'temperature': {'warning': 50, 'threshold': 60}}))
        _collect(p, run_cycle, temps_mc=(52_000,))
        assert _latest_status() == "warning"
        _collect(p, run_cycle, temps_mc=(65_000,))
        assert _latest_status() == "failed"

    async def test_a_host_with_no_zones_stays_online(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['temperature']))
        _collect(p, run_cycle, temps_mc=())
        assert _latest_status() == "online"
        assert _latest_metric("temp_c") is None

    def test_zone_cards_get_their_own_row(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['temperature']))
        assert ['sensors'] in p.UI_SPEC['layout']


class TestInterruptsModule:
    async def test_rates_are_the_delta_between_snapshots(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['interrupts']))
        _collect(p, run_cycle, intr_delta=1500)
        assert _latest_status() == "online"
        assert _latest_metric("irq_per_sec") == pytest.approx(1500.0)
        assert _latest_metric("ctxt_per_sec") == pytest.approx(2000.0)

    async def test_high_rate_crosses_thresholds(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'interrupts': {'warning': 500, 'threshold': 1000}}))
        _collect(p, run_cycle, intr_delta=1200)
        assert _latest_status() == "failed"

    async def test_unexpected_output_fails(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['interrupts']))
        run_cycle(p, lambda c: CmdResult(0, "cpu 1 2 3\n", ""))
        assert _latest_status() == "failed"

    def test_samples_separately_from_the_cpu_module(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['cpu', 'interrupts']))
        assert len(p.commands()) == 2


class TestGpuModule:
    async def test_peak_across_cards_is_reported(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['gpu']))
        _collect(p, run_cycle, gpu_util=40.0, gpu_temp=60.0, gpu_count=2)
        assert _latest_status() == "online"
        assert _latest_metric("gpu_util") == pytest.approx(40.0)
        assert _latest_metric("gpu1_util") == pytest.approx(40.0)
        assert _latest_metric("gpu_temp") == pytest.approx(60.0)

    async def test_hot_gpu_fails(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={'gpu': {'temp_threshold': 55}}))
        _collect(p, run_cycle, gpu_temp=60.0)
        assert _latest_status() == "failed"

    async def test_missing_nvidia_smi_is_offline(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['gpu']))
        run_cycle(p, lambda c: CmdResult(127, "", "nvidia-smi: command not found"))
        assert _latest_status() == "offline"

    async def test_repeated_timeouts_suspend_the_probe(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'gpu': {'timeout_trip': 2, 'suspend_seconds': 600}}))
        timed_out = lambda c: CmdResult(1, "", "Command timed out after 15s")
        run_cycle(p, timed_out)
        assert _latest_status() == "failed"
        run_cycle(p, timed_out)
        assert _latest_status() == "offline"
        assert p.commands() == []           # breaker open: nothing issued at all
        run_cycle(p, timed_out)
        assert _latest_status() == "offline"

    async def test_a_suspended_gpu_does_not_stall_other_modules(self, make_plugin, run_cycle):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules={
            'gpu': {'timeout_trip': 1}, 'memory': {}}))
        results = [CmdResult(0, _meminfo(8_000_000), ""), CmdResult(1, "", "timed out after 15s")]
        run_cycle(p, lambda c: results.pop(0))
        assert _latest_status() == "offline"
        assert len(p.commands()) == 1       # memory only; the gpu breaker is open
        _collect(p, run_cycle, avail_kb=8_000_000)
        assert _latest_metric("memory_pct") == pytest.approx(50.0)


class TestUiSpec:
    def test_spec_covers_enabled_modules_only(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['memory', 'oom']))
        spec = p.UI_SPEC
        assert set(spec['charts']) == {'memory_chart', 'oom_chart'}
        assert 'load_1m_card' not in spec['cards']
        assert spec['layout'][0][0] == 'host_card'
        assert spec['layout'][-1] == ['events']

    def test_gpu_repeat_card_gets_its_own_row(self, make_plugin):
        p = make_plugin(SystemStats, dict(BASE_CFG, modules=['gpu']))
        spec = p.UI_SPEC
        assert 'gpus' not in spec['layout'][0]
        assert ['gpus'] in spec['layout']

    def test_every_card_and_chart_has_a_layout_cell(self, plugin):
        spec = plugin.UI_SPEC
        placed = {name for row in spec['layout'] for name in row}
        assert set(spec['cards']) <= placed
        assert set(spec['charts']) <= placed


class TestWorst:
    def test_orders_by_severity(self):
        assert _worst(['online', 'warning', 'failed']) == 'failed'
        assert _worst(['online', 'offline']) == 'offline'
        assert _worst(['offline', 'warning']) == 'warning'
        assert _worst([]) == 'online'
