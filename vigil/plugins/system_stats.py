from typing import Dict, Any, Optional, Tuple
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card, history_chart
from vigil.core.ui.theme import STATUS_COLORS

# Single SSH command: first CPU sample, memory, thermal zones, load averages,
# core count, 1s sleep, second CPU sample. Each section identified by line prefix.
_COLLECT_CMD = (
    "{ head -1 /proc/stat; "
    "grep -E 'MemTotal:|MemAvailable:' /proc/meminfo; "
    "for f in /sys/class/thermal/thermal_zone*/temp; "
    "do [ -f \"$f\" ] && echo \"TEMP:$(cat $f)\"; done; "
    "echo \"LOAD:$(cat /proc/loadavg)\"; "
    "echo \"CPUS:$(nproc)\"; "
    "sleep 1; "
    "head -1 /proc/stat; }"
)

_SEVERITY = {'online': 0, 'warning': 1, 'failed': 2}


def _parse_cpu_line(line: str) -> Tuple[int, int]:
    """Return (total_jiffies, idle_jiffies) from a /proc/stat cpu line."""
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


def _level_for(value: float, warning: float, failed: float) -> str:
    """Return 'failed', 'warning', or 'online' based on two thresholds."""
    if value >= failed:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


def _fmt_gb(gb: float) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB"


class SystemStatsPlugin(BasePlugin):
    """
    Monitors CPU usage, memory usage, temperature, and load averages over SSH.

    Uses /proc/stat (two 1-second samples) for CPU, /proc/meminfo for memory,
    /sys/class/thermal/thermal_zone*/temp for temperature, and /proc/loadavg
    for the 1m/5m/15m load averages — no extra tools required on the remote host.

    Temperature monitoring is skipped gracefully on hosts with no thermal zones.
    Load average thresholds are optional; when unset, load metrics are collected
    but do not affect plugin status.

    Config options:
      cpu_warning      CPU % that triggers warning (default: 70)
      cpu_threshold    CPU % that triggers failed  (default: 85)
      memory_warning   Memory % that triggers warning (default: 75)
      memory_threshold Memory % that triggers failed  (default: 90)
      temp_warning     Temperature °C that triggers warning (default: 70)
      temp_threshold   Temperature °C that triggers failed  (default: 80)
      load_warning     1m load as % of CPU cores that triggers warning (default: unset)
      load_threshold   1m load as % of CPU cores that triggers failed  (default: unset)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.cpu_warning      = int(config.get('cpu_warning',      70))
        self.cpu_threshold    = int(config.get('cpu_threshold',    85))
        self.memory_warning   = int(config.get('memory_warning',   75))
        self.memory_threshold = int(config.get('memory_threshold', 90))
        self.temp_warning     = int(config.get('temp_warning',     70))
        self.temp_threshold   = int(config.get('temp_threshold',   80))
        # Load thresholds are optional — meaningful values depend on CPU count
        self.load_warning   = float(config['load_warning'])   if 'load_warning'   in config else None
        self.load_threshold = float(config['load_threshold'])  if 'load_threshold'  in config else None
        self.ssh_collector = self.internal_modules['collectors']['ssh']
        self.db_logger     = self.internal_modules['loggers']['db_logs']
        self.db_metrics    = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_COLLECT_CMD)
        if ret != 0:
            self.db_logger.write(f"Stats collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        lines = stdout.splitlines()
        cpu_lines      = [l for l in lines if l.startswith('cpu ')]
        mem_total_line = next((l for l in lines if l.startswith('MemTotal:')),     None)
        mem_avail_line = next((l for l in lines if l.startswith('MemAvailable:')), None)
        temp_lines     = [l for l in lines if l.startswith('TEMP:')]
        load_line      = next((l for l in lines if l.startswith('LOAD:')), None)
        cpus_line      = next((l for l in lines if l.startswith('CPUS:')), None)

        if len(cpu_lines) < 2 or not mem_total_line or not mem_avail_line:
            self.db_logger.write(f"Incomplete output from remote host: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        try:
            cpu_pct = _cpu_pct(cpu_lines[0], cpu_lines[1])

            mem_total_kb    = int(mem_total_line.split()[1])
            mem_avail_kb    = int(mem_avail_line.split()[1])
            mem_used_kb     = mem_total_kb - mem_avail_kb
            memory_pct      = 100.0 * mem_used_kb / mem_total_kb if mem_total_kb > 0 else 0.0
            memory_total_gb = mem_total_kb / (1024 ** 2)
            memory_used_gb  = mem_used_kb  / (1024 ** 2)

            temp_c: Optional[float] = None
            if temp_lines:
                temps_mc = [int(l.removeprefix('TEMP:')) for l in temp_lines
                            if l.removeprefix('TEMP:').strip().isdigit()]
                if temps_mc:
                    temp_c = max(temps_mc) / 1000.0

            load_pct_1m = load_pct_5m = load_pct_15m = None
            if load_line:
                cpu_count = max(1, int(cpus_line.removeprefix('CPUS:').strip())) if cpus_line else 1
                parts = load_line.removeprefix('LOAD:').split()
                load_pct_1m  = float(parts[0]) / cpu_count * 100.0
                load_pct_5m  = float(parts[1]) / cpu_count * 100.0
                load_pct_15m = float(parts[2]) / cpu_count * 100.0

        except (ValueError, IndexError, ZeroDivisionError) as e:
            self.db_logger.write(f"Failed to parse stats output: {e}", level="ERROR")
            self.set_status('failed')
            return

        self.db_metrics.metric('cpu_pct',         cpu_pct)
        self.db_metrics.metric('memory_pct',      memory_pct)
        self.db_metrics.metric('memory_used_gb',  memory_used_gb)
        self.db_metrics.metric('memory_total_gb', memory_total_gb)
        if temp_c is not None:
            self.db_metrics.metric('temp_c', temp_c)
        if load_pct_1m is not None:
            self.db_metrics.metric('load_pct_1m',  load_pct_1m)
            self.db_metrics.metric('load_pct_5m',  load_pct_5m)
            self.db_metrics.metric('load_pct_15m', load_pct_15m)

        levels = [
            _level_for(cpu_pct,    self.cpu_warning,    self.cpu_threshold),
            _level_for(memory_pct, self.memory_warning, self.memory_threshold),
        ]
        if temp_c is not None:
            levels.append(_level_for(temp_c, self.temp_warning, self.temp_threshold))
        if load_pct_1m is not None and self.load_warning is not None and self.load_threshold is not None:
            levels.append(_level_for(load_pct_1m, self.load_warning, self.load_threshold))

        overall = max(levels, key=lambda s: _SEVERITY[s])

        temp_str = f"{temp_c:.1f}°C" if temp_c is not None else "N/A"
        load_str = (f"{load_pct_1m:.0f}% / {load_pct_5m:.0f}% / {load_pct_15m:.0f}%"
                    if load_pct_1m is not None else "N/A")
        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"CPU {cpu_pct:.1f}% (warn {self.cpu_warning}% / fail {self.cpu_threshold}%), "
            f"MEM {memory_pct:.1f}% ({_fmt_gb(memory_used_gb)}/{_fmt_gb(memory_total_gb)}, "
            f"warn {self.memory_warning}% / fail {self.memory_threshold}%), "
            f"TEMP {temp_str} (warn {self.temp_warning}°C / fail {self.temp_threshold}°C), "
            f"LOAD {load_str}",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric

        def latest(metric_name):
            return Metric.select().where(
                (Metric.collector == self.name) & (Metric.metric_name == metric_name)
            ).order_by(Metric.timestamp.desc()).first()

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()

            cpu_label      = info_card('CPU',      '-- %')
            mem_pct_label  = info_card('MEMORY',   '-- %')
            mem_used_label = info_card('MEM USED', '--')
            temp_label     = info_card('TEMP',     'N/A')
            load_1m_label  = info_card('LOAD 1M',  '-- %')
            load_5m_label  = info_card('LOAD 5M',  '-- %')
            load_15m_label = info_card('LOAD 15M', '-- %')

            def update_cards():
                cpu       = latest('cpu_pct')
                mem_pct   = latest('memory_pct')
                mem_used  = latest('memory_used_gb')
                mem_total = latest('memory_total_gb')
                temp      = latest('temp_c')
                load_1m   = latest('load_pct_1m')
                load_5m   = latest('load_pct_5m')
                load_15m  = latest('load_pct_15m')

                if cpu:
                    cpu_label.text = f'{cpu.value:.1f}%'
                    cpu_label.style(f'color: {STATUS_COLORS[_level_for(cpu.value, self.cpu_warning, self.cpu_threshold)]}')

                if mem_pct:
                    mem_pct_label.text = f'{mem_pct.value:.1f}%'
                    mem_pct_label.style(f'color: {STATUS_COLORS[_level_for(mem_pct.value, self.memory_warning, self.memory_threshold)]}')

                if mem_used and mem_total:
                    mem_used_label.text = f'{_fmt_gb(mem_used.value)} / {_fmt_gb(mem_total.value)}'

                if temp:
                    temp_label.text = f'{temp.value:.1f}°C'
                    temp_label.style(f'color: {STATUS_COLORS[_level_for(temp.value, self.temp_warning, self.temp_threshold)]}')

                if load_1m:
                    load_1m_label.text = f'{load_1m.value:.0f}%'
                    if self.load_warning is not None and self.load_threshold is not None:
                        load_1m_label.style(f'color: {STATUS_COLORS[_level_for(load_1m.value, self.load_warning, self.load_threshold)]}')

                if load_5m:
                    load_5m_label.text = f'{load_5m.value:.0f}%'

                if load_15m:
                    load_15m_label.text = f'{load_15m.value:.0f}%'

            ui.timer(5.0, update_cards)

        history_chart('CPU USAGE (%)',    self.name, 'cpu_pct')
        history_chart('MEMORY USAGE (%)', self.name, 'memory_pct')
        history_chart('TEMPERATURE (°C)', self.name, 'temp_c')
        history_chart('LOAD AVERAGE (%)',  self.name, 'load_pct_1m')
        self.internal_modules['ui']['logs_table']()
