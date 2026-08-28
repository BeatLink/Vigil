"""One monitor for the basic health signals of a host — CPU, memory, load
average, temperature, interrupts, GPU and OOM kills — instead of one plugin
instance per signal.

Each signal is a module, configured and rendered independently via the
`modules` config block. Every module is opt-in — one that isn't declared is
off — so a target only collects (and only shows) what it is asked for. Modules stay pure — each declares its own commands, parses
its own results into metrics/logs/a status, and contributes its own cards and
charts. The plugin concatenates their commands, slices the results back out
positionally, and reports the worst module status as the overall one.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from vigil.plugins.base.module_plugin import (
    LOG_LEVEL as _LOG_LEVEL, Module as _Module, ModularPlugin, SEVERITY as _SEVERITY,
    module_options, worst_status as _worst,
)
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.plugins.base.plugin_helpers import (
    level_for as _level_for, format_bytes as _fmt_gb,
)
from vigil_agent.protocol import StreamSpec


def _parse_cpu_line(line: str) -> Tuple[int, int]:
    parts = line.split()
    user, nice, system, idle = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
    iowait  = int(parts[5]) if len(parts) > 5 else 0
    irq     = int(parts[6]) if len(parts) > 6 else 0
    softirq = int(parts[7]) if len(parts) > 7 else 0
    steal   = int(parts[8]) if len(parts) > 8 else 0
    total   = user + nice + system + idle + iowait + irq + softirq + steal
    return total, idle + iowait


def _cpu_pct(line1: str, line2: str) -> float:
    total1, idle1 = _parse_cpu_line(line1)
    total2, idle2 = _parse_cpu_line(line2)
    delta_total = total2 - total1
    delta_idle  = idle2  - idle1
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (1.0 - delta_idle / delta_total)))


class _CpuModule(_Module):
    """CPU utilization from two /proc/stat samples a second apart, taken on
    the target so the sleep costs one round trip rather than two."""

    key = 'cpu'

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = float(options.get('warning',   70))
        self.threshold = float(options.get('threshold', 85))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule = f'system_stats_cpu_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    def commands(self) -> List[Command]:
        return [Command("{ head -1 /proc/stat; sleep 1; head -1 /proc/stat; }")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"CPU collection failed: {stderr}")

        cpu_lines = [l for l in stdout.splitlines() if l.startswith('cpu ')]
        if len(cpu_lines) < 2:
            return CollectResult.failed(f"Incomplete CPU output: {stdout!r}")

        try:
            cpu_pct = _cpu_pct(cpu_lines[0], cpu_lines[1])
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse CPU output: {e}")

        status = _level_for(cpu_pct, self.warning, self.threshold)
        return CollectResult(
            metrics={'cpu_pct': cpu_pct},
            logs=[(
                f"CPU {cpu_pct:.1f}% (warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {'cpu_card': {'metric': 'cpu_pct', 'title': 'CPU', 'format': 'percent1',
                             'color': self._color_rule}}

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'cpu_chart': {'metric': 'cpu_pct', 'title': 'CPU USAGE (%)'}}


class _MemoryModule(_Module):
    """RAM usage from /proc/meminfo, counting MemAvailable as free so the
    filesystem cache is not reported as used."""

    key = 'memory'

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = float(options.get('warning',   75))
        self.threshold = float(options.get('threshold', 90))

        from vigil.core.ui.spec import register_color_rule, threshold_color, register_item_formatter
        self._color_rule = f'system_stats_memory_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))
        self._used_format = f'system_stats_memory_used_{plugin.id}'
        register_item_formatter(self._used_format)(_format_memory_used)

    def commands(self) -> List[Command]:
        return [Command("grep -E 'MemTotal:|MemAvailable:' /proc/meminfo")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Memory collection failed: {stderr}")

        lines = stdout.splitlines()
        total_line = next((l for l in lines if l.startswith('MemTotal:')),     None)
        avail_line = next((l for l in lines if l.startswith('MemAvailable:')), None)
        if not total_line or not avail_line:
            return CollectResult.failed(f"Incomplete memory output: {stdout!r}")

        try:
            total_kb = int(total_line.split()[1])
            avail_kb = int(avail_line.split()[1])
            used_kb  = total_kb - avail_kb
            memory_pct      = 100.0 * used_kb / total_kb if total_kb > 0 else 0.0
            memory_total_gb = total_kb / (1024 ** 2)
            memory_used_gb  = used_kb  / (1024 ** 2)
        except (ValueError, IndexError, ZeroDivisionError) as e:
            return CollectResult.failed(f"Failed to parse memory output: {e}")

        status = _level_for(memory_pct, self.warning, self.threshold)
        return CollectResult(
            metrics={
                'memory_pct':      memory_pct,
                'memory_used_gb':  memory_used_gb,
                'memory_total_gb': memory_total_gb,
            },
            logs=[(
                f"MEM {memory_pct:.1f}% ({_fmt_gb(memory_used_gb)} / {_fmt_gb(memory_total_gb)}, "
                f"warn {self.warning:g}% / fail {self.threshold:g}%)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'mem_pct_card': {'metric': 'memory_pct', 'title': 'MEMORY',
                             'format': 'percent1_plain_dash', 'color': self._color_rule},
            'mem_used_card': {'title': 'MEM USED', 'metrics': ['memory_used_gb', 'memory_total_gb'],
                              'format_fn': self._used_format},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'memory_chart': {'metric': 'memory_pct', 'title': 'MEMORY USAGE (%)'}}


def _format_memory_used(values: Dict[str, Any]) -> str:
    used, total = values.get('memory_used_gb'), values.get('memory_total_gb')
    if used is None or total is None:
        return '--'
    return f'{_fmt_gb(used)} / {_fmt_gb(total)}'


class _LoadModule(_Module):
    """Load average from /proc/loadavg, normalized by core count so 100%
    means the host is exactly at capacity. Thresholds are optional; without
    both, load is collected and charted but never changes the status."""

    key = 'load'

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = float(options['warning'])   if 'warning'   in options else None
        self.threshold = float(options['threshold']) if 'threshold' in options else None

        self._color_rule = None
        if self.warning is not None and self.threshold is not None:
            from vigil.core.ui.spec import register_color_rule, threshold_color
            self._color_rule = f'system_stats_load_{plugin.id}'
            register_color_rule(self._color_rule)(
                threshold_color(warning=self.warning, threshold=self.threshold))

    def commands(self) -> List[Command]:
        return [Command('echo "LOAD:$(cat /proc/loadavg)"; echo "CPUS:$(nproc)"')]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Load collection failed: {stderr}")

        lines = stdout.splitlines()
        load_line = next((l for l in lines if l.startswith('LOAD:')), None)
        cpus_line = next((l for l in lines if l.startswith('CPUS:')), None)
        if not load_line:
            return CollectResult.failed(f"Incomplete load output: {stdout!r}")

        try:
            cpu_count    = max(1, int(cpus_line.removeprefix('CPUS:').strip())) if cpus_line else 1
            parts        = load_line.removeprefix('LOAD:').split()
            load_pct_1m  = float(parts[0]) / cpu_count * 100.0
            load_pct_5m  = float(parts[1]) / cpu_count * 100.0
            load_pct_15m = float(parts[2]) / cpu_count * 100.0
        except (ValueError, IndexError) as e:
            return CollectResult.failed(f"Failed to parse load output: {e}")

        if self.warning is not None and self.threshold is not None:
            status = _level_for(load_pct_1m, self.warning, self.threshold)
        else:
            status = 'online'

        return CollectResult(
            metrics={
                'load_pct_1m':  load_pct_1m,
                'load_pct_5m':  load_pct_5m,
                'load_pct_15m': load_pct_15m,
            },
            logs=[(
                f"LOAD {load_pct_1m:.0f}% / {load_pct_5m:.0f}% / {load_pct_15m:.0f}% (1m/5m/15m, "
                f"{cpu_count} cores)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        load_1m_card = {'metric': 'load_pct_1m', 'title': 'LOAD 1M', 'format': 'percent0_plain_dash'}
        if self._color_rule:
            load_1m_card['color'] = self._color_rule
        return {
            'load_1m_card':  load_1m_card,
            'load_5m_card':  {'metric': 'load_pct_5m',  'title': 'LOAD 5M',  'format': 'percent0_plain_dash'},
            'load_15m_card': {'metric': 'load_pct_15m', 'title': 'LOAD 15M', 'format': 'percent0_plain_dash'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'load_chart': {'metric': 'load_pct_1m', 'title': 'LOAD AVERAGE (%)'}}


class _OomModule(_Module):
    """Kernel OOM kills from /proc/vmstat's oom_kill counter, plus — on an
    agent-backed host — the kernel journal line the OOM killer itself emits.
    The counter remains the authority on totals; the journal only makes a kill
    visible immediately and carries the process name, which the counter can't."""

    key = 'oom'

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.alert_for  = int(options.get('alert_for', 3))
        self.is_warning = bool(options.get('is_warning', False))
        self._last_total: Optional[int] = None
        self._since_kill: Optional[int] = None

        from vigil.core.ui.spec import register_color_rule
        self._color_rule = f'system_stats_oom_recent_{plugin.id}'

        @register_color_rule(self._color_rule)
        def _recent_color(v, _is_warning=self.is_warning):
            if v is None:
                return None
            return 'online' if v == 0 else ('warning' if _is_warning else 'failed')

    @property
    def _kill_status(self) -> str:
        return 'warning' if self.is_warning else 'failed'

    def subscriptions(self) -> List[StreamSpec]:
        return [StreamSpec(
            id=self.plugin.id,
            kind='journal',
            params={'kernel': True, 'grep': 'Out of memory'},
        )]

    def parse_event(self, payload: Dict[str, Any]) -> Optional[CollectResult]:
        message = str(payload.get('message', '')).strip()
        if not message:
            return None
        self._since_kill = 0
        return CollectResult(
            logs=[(f"OOM killer fired: {message}", "ERROR")],
            status=self._kill_status,
        )

    def commands(self) -> List[Command]:
        return [Command('cat /proc/vmstat')]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/vmstat: {stderr}")

        total = _extract_counter(stdout, 'oom_kill')
        if total is None:
            return CollectResult.failed(
                "No 'oom_kill' counter in /proc/vmstat (kernel too old?)",
                level="WARNING", status='offline')

        metrics = {'oom_kills_total': float(total)}
        previous, self._last_total = self._last_total, total

        if previous is None:
            return CollectResult(
                metrics=metrics,
                logs=[(f"Baseline established: {total} OOM kill(s) since boot", "INFO")],
                status='online',
            )

        if total < previous:
            return CollectResult(
                metrics=metrics,
                logs=[(f"OOM counter reset ({previous} -> {total}); host likely rebooted", "INFO")],
                status='online',
            )

        delta = total - previous
        metrics['oom_kills_new'] = float(delta)

        if delta > 0:
            self._since_kill = 0
            return CollectResult(
                metrics=metrics,
                logs=[(
                    f"{delta} OOM kill(s) since last check — the kernel terminated "
                    f"process(es) to reclaim memory ({total} total since boot)",
                    "WARNING" if self.is_warning else "ERROR",
                )],
                status=self._kill_status,
            )

        if self._since_kill is not None:
            self._since_kill += 1
            if self._since_kill < self.alert_for:
                return CollectResult(
                    metrics=metrics,
                    logs=[(
                        f"No new OOM kills ({self._since_kill}/{self.alert_for} "
                        f"collections since the last one)",
                        "WARNING",
                    )],
                    status='warning',
                )
            self._since_kill = None

        return CollectResult(
            metrics=metrics,
            logs=[(f"No OOM kills ({total} total since boot)", "INFO")],
            status='online',
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'oom_total_card': {'metric': 'oom_kills_total', 'title': 'OOM KILLS (BOOT)',
                               'format': 'count_comma_rounded'},
            'oom_recent_card': {'metric': 'oom_kills_new', 'title': 'OOM SINCE LAST CHECK',
                                'format': 'count_comma_rounded', 'color': self._color_rule},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'oom_chart': {'metric': 'oom_kills_total', 'title': 'OOM KILLS SINCE BOOT'}}


def _sanitize_zone(name: str) -> str:
    return ''.join(c if c.isalnum() or c == '_' else '_' for c in name.lower())


class _TemperatureModule(_Module):
    """Thermal-zone temperatures from /sys/class/thermal, reporting the hottest
    zone as the status plus one metric per zone. A host with no thermal zones —
    a VM, typically — stays online with no metric rather than reporting a
    problem it cannot see."""

    key = 'temperature'

    _QUERY = (
        "for d in /sys/class/thermal/thermal_zone*; do "
        "  [ -f \"$d/temp\" ] || continue; "
        "  type=$(cat \"$d/type\" 2>/dev/null || echo unknown); "
        "  temp=$(cat \"$d/temp\" 2>/dev/null || echo 0); "
        "  echo \"SENSOR:${type}:${temp}\"; "
        "done"
    )

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = float(options.get('warning',   70))
        self.threshold = float(options.get('threshold', 80))

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._item_color_rule = f'system_stats_temp_zone_{plugin.id}'
        register_item_color_rule(self._item_color_rule)(
            lambda item: _level_for(item.get('value') or 0.0, self.warning, self.threshold))
        self._color_rule = f'system_stats_temp_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    def commands(self) -> List[Command]:
        return [Command(self._QUERY)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Temperature collection failed: {stderr}")

        sensors: Dict[str, float] = {}
        for line in stdout.splitlines():
            if not line.startswith('SENSOR:'):
                continue
            parts = line.split(':', 2)
            if len(parts) != 3:
                continue
            zone_type, temp_mc = parts[1], parts[2].strip()
            try:
                temp_c = int(temp_mc) / 1000.0
            except (ValueError, TypeError):
                continue
            key = _sanitize_zone(zone_type)
            sensors[key] = max(sensors.get(key, 0.0), temp_c)

        if not sensors:
            return CollectResult(logs=[("No thermal zones found — skipping", "INFO")],
                                 status='online')

        max_temp = max(sensors.values())
        metrics = {'temp_c': max_temp}
        for key, temp_c in sensors.items():
            metrics[f'temp_zone_{key}'] = temp_c

        status = _level_for(max_temp, self.warning, self.threshold)
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"Max {max_temp:.1f}°C across {len(sensors)} zone(s) "
                f"(warn {self.warning:g}°C / fail {self.threshold:g}°C)",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'temp_card': {'metric': 'temp_c', 'title': 'MAX TEMP', 'format': 'temp_c1',
                          'color': self._color_rule},
            'sensors': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'temp_zone_', 'metrics_suffix': '',
                    'item_format': 'temp_c1',
                    'item_color_by': self._item_color_rule,
                    'label_transform': 'spaces_upper',
                    'container': 'cards',
                    'empty_text': 'No thermal zones found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['temp_card']

    def rows(self) -> List[List[str]]:
        return [['sensors']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'temp_chart': {'metric': 'temp_c', 'title': 'TEMPERATURE (°C)'}}


def _extract_counter(block: str, key: str) -> Optional[int]:
    for line in block.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key:
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


class _InterruptsModule(_Module):
    """Interrupt and context-switch rates from two /proc/stat samples a second
    apart. Takes its own sample rather than sharing the cpu module's, so each
    module stays independently enableable."""

    key = 'interrupts'

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.warning   = float(options.get('warning',   20000))
        self.threshold = float(options.get('threshold', 50000))

        from vigil.core.ui.spec import register_color_rule, threshold_color
        self._color_rule = f'system_stats_interrupts_{plugin.id}'
        register_color_rule(self._color_rule)(
            threshold_color(warning=self.warning, threshold=self.threshold))

    def commands(self) -> List[Command]:
        return [Command("cat /proc/stat && sleep 1 && echo '---SNAP---' && cat /proc/stat")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Failed to read /proc/stat: {stderr}")

        halves = stdout.split('---SNAP---')
        if len(halves) < 2:
            return CollectResult.failed("Unexpected /proc/stat output format")

        intr1 = _extract_counter(halves[0], 'intr')
        intr2 = _extract_counter(halves[1], 'intr')
        ctxt1 = _extract_counter(halves[0], 'ctxt')
        ctxt2 = _extract_counter(halves[1], 'ctxt')

        if intr1 is None or intr2 is None:
            return CollectResult.failed("Could not read 'intr' from /proc/stat")

        irq_rate = max(0.0, float(intr2 - intr1))
        metrics = {'irq_per_sec': irq_rate}
        if ctxt1 is not None and ctxt2 is not None:
            metrics['ctxt_per_sec'] = max(0.0, float(ctxt2 - ctxt1))

        status = _level_for(irq_rate, self.warning, self.threshold)
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{irq_rate:.0f} interrupts/sec (warn {self.warning:g} / fail {self.threshold:g})",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'irq_card': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS/S',
                         'format': 'count_comma_rounded', 'color': self._color_rule},
            'ctxt_card': {'metric': 'ctxt_per_sec', 'title': 'CTX SWITCH/S',
                          'format': 'count_comma_rounded'},
        }

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {
            'irq_chart': {'metric': 'irq_per_sec', 'title': 'INTERRUPTS / SEC'},
            'ctxt_chart': {'metric': 'ctxt_per_sec', 'title': 'CONTEXT SWITCHES / SEC'},
        }


class _GpuModule(_Module):
    """NVIDIA GPU utilization, VRAM and temperature via nvidia-smi, reporting
    the peak across cards as the status plus one metric family per card.

    A wedged NVIDIA driver leaves nvidia-smi in uninterruptible sleep, where the
    connector's terminate/kill has no effect, so every poll would strand another
    process until the host reboots. Repeated timeouts therefore suspend the probe
    instead of adding one stuck process per interval."""

    key = 'gpu'

    _QUERY = (
        "nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu "
        "--format=csv,noheader,nounits"
    )
    _COMMAND_TIMEOUT = 15.0

    def __init__(self, plugin: 'SystemStats', options: Dict[str, Any]):
        super().__init__(plugin, options)
        self.util_warning   = float(options.get('util_warning',   85))
        self.util_threshold = float(options.get('util_threshold', 95))
        self.mem_warning    = float(options.get('mem_warning',    85))
        self.mem_threshold  = float(options.get('mem_threshold',  95))
        self.temp_warning   = float(options.get('temp_warning',   80))
        self.temp_threshold = float(options.get('temp_threshold', 90))
        self.timeout_trip     = int(options.get('timeout_trip',      2))
        self.suspend_seconds  = float(options.get('suspend_seconds', 1800.0))
        self._consecutive_timeouts = 0
        self._suspended_until = 0.0

        from vigil.core.ui.spec import register_item_color_rule, register_color_rule, threshold_color
        self._item_color_rule = f'system_stats_gpu_util_{plugin.id}'
        register_item_color_rule(self._item_color_rule)(
            lambda item: _level_for(item.get('value') or 0.0, self.util_warning, self.util_threshold))
        self._util_color_rule = f'system_stats_gpu_util_card_{plugin.id}'
        register_color_rule(self._util_color_rule)(
            threshold_color(warning=self.util_warning, threshold=self.util_threshold))
        self._mem_color_rule = f'system_stats_gpu_mem_card_{plugin.id}'
        register_color_rule(self._mem_color_rule)(
            threshold_color(warning=self.mem_warning, threshold=self.mem_threshold))
        self._temp_color_rule = f'system_stats_gpu_temp_card_{plugin.id}'
        register_color_rule(self._temp_color_rule)(
            threshold_color(warning=self.temp_warning, threshold=self.temp_threshold))

    def commands(self) -> List[Command]:
        if time.monotonic() < self._suspended_until:
            return []
        return [Command(self._QUERY, timeout=self._COMMAND_TIMEOUT)]

    def _suspended_result(self) -> CollectResult:
        """Reports the tripped breaker as offline, the same status an absent GPU gets."""
        minutes = self.suspend_seconds / 60.0
        return CollectResult.failed(
            f"nvidia-smi timed out {self._consecutive_timeouts}x and could not be killed — the "
            f"driver is wedged and only a reboot clears it; probe suspended for {minutes:.0f}m",
            level="WARNING", status='offline')

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if not results:
            return self._suspended_result()

        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        if ret != 0 and 'timed out after' in stderr.lower():
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self.timeout_trip:
                self._suspended_until = time.monotonic() + self.suspend_seconds
                return self._suspended_result()
            return CollectResult.failed(f"GPU collection failed: {stderr}")
        self._consecutive_timeouts = 0

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined
                         or "couldn't communicate" in combined or 'no devices' in combined):
            return CollectResult.failed("nvidia-smi unavailable or no NVIDIA GPU present",
                                        level="WARNING", status='offline')
        if ret != 0:
            return CollectResult.failed(f"GPU collection failed: {stderr}")

        metrics: Dict[str, float] = {}
        max_util = max_mem_pct = max_temp = 0.0
        count = 0
        for line in stdout.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) != 5:
                continue
            try:
                idx       = int(parts[0])
                util      = float(parts[1])
                mem_used  = float(parts[2])
                mem_total = float(parts[3])
                temp      = float(parts[4])
            except ValueError:
                continue

            mem_pct = (100.0 * mem_used / mem_total) if mem_total > 0 else 0.0
            metrics[f'gpu{idx}_util'] = util
            metrics[f'gpu{idx}_mem_pct'] = mem_pct
            metrics[f'gpu{idx}_temp'] = temp

            max_util    = max(max_util, util)
            max_mem_pct = max(max_mem_pct, mem_pct)
            max_temp    = max(max_temp, temp)
            count += 1

        if count == 0:
            return CollectResult.failed(f"No GPUs parsed from output: {stdout!r}",
                                        level="WARNING", status='offline')

        metrics['gpu_util'] = max_util
        metrics['gpu_mem_pct'] = max_mem_pct
        metrics['gpu_temp'] = max_temp

        status = _worst([
            _level_for(max_util,    self.util_warning, self.util_threshold),
            _level_for(max_mem_pct, self.mem_warning,  self.mem_threshold),
            _level_for(max_temp,    self.temp_warning, self.temp_threshold),
        ])
        return CollectResult(
            metrics=metrics,
            logs=[(
                f"{count} GPU(s): peak {max_util:.0f}% util, {max_mem_pct:.0f}% VRAM, "
                f"{max_temp:.0f}°C",
                _LOG_LEVEL[status],
            )],
            status=status,
        )

    def cards(self) -> Dict[str, Dict[str, Any]]:
        return {
            'gpu_util_card': {'metric': 'gpu_util', 'title': 'GPU', 'format': 'percent0',
                              'color': self._util_color_rule},
            'gpu_mem_card': {'metric': 'gpu_mem_pct', 'title': 'VRAM', 'format': 'percent0',
                             'color': self._mem_color_rule},
            'gpu_temp_card': {'metric': 'gpu_temp', 'title': 'GPU TEMP', 'format': 'temp_c0',
                              'color': self._temp_color_rule},
            'gpus': {
                'repeat': {
                    'source': 'metrics_prefix',
                    'metrics_prefix': 'gpu', 'metrics_suffix': '_util',
                    'metrics_exclude': ['gpu_util'],
                    'item_format': 'percent0',
                    'item_color_by': self._item_color_rule,
                    'item_label_prefix': 'GPU ',
                    'container': 'cards',
                    'empty_text': 'No GPUs found',
                },
            },
        }

    def card_row(self) -> List[str]:
        return ['gpu_util_card', 'gpu_mem_card', 'gpu_temp_card']

    def rows(self) -> List[List[str]]:
        return [['gpus']]

    def charts(self) -> Dict[str, Dict[str, Any]]:
        return {'gpu_chart': {'metric': 'gpu_util', 'title': 'GPU UTILIZATION (%)'}}


# Canonical order; also the set of names `modules` may enable.
_MODULE_TYPES = [_CpuModule, _MemoryModule, _LoadModule, _TemperatureModule,
                 _InterruptsModule, _GpuModule, _OomModule]


def _module_options(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Resolve this plugin's `modules` block to {module key: options}."""
    return module_options('system_stats', config, _MODULE_TYPES)


class SystemStats(ModularPlugin):
    MODULE_TYPES = _MODULE_TYPES
    MODULE_LABEL = 'system_stats'
