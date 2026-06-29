from typing import Dict, Any, List, Optional
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card
from vigil.core.ui.theme import STATUS_COLORS

_SEVERITY = {'online': 0, 'warning': 1, 'failed': 2}


def _parse_ps_output(stdout: str) -> List[Dict]:
    """Parse `ps -eo pid,user,pcpu,pmem,comm` output (first line is header)."""
    processes = []
    lines = stdout.strip().splitlines()
    if len(lines) < 2:
        return processes
    for line in lines[1:]:  # skip header
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            processes.append({
                'pid':     int(parts[0]),
                'user':    parts[1],
                'cpu':     float(parts[2]),
                'mem':     float(parts[3]),
                'command': parts[4].strip(),
            })
        except (ValueError, IndexError):
            continue
    return processes


def _level_for(value: float, warning: float, failed: float) -> str:
    if value >= failed:
        return 'failed'
    if value >= warning:
        return 'warning'
    return 'online'


_DEFAULT_LAYOUT = {
    'grid_columns': 3,
    'widgets': {
        'host_card':    {'col_span': 1},
        'count_card':   {'col_span': 1},
        'top_cpu_card': {'col_span': 1},
        'table':        {'col_span': 3},
        'logs':         {'col_span': 3},
    }
}


class ProcessesPlugin(BasePlugin):
    """
    Monitors running processes over SSH via `ps`.

    Collects the top N processes sorted by CPU usage.  Process data is stored
    in memory and refreshed each cycle; the table in the UI sorts and refreshes
    automatically.  Per-row SIGTERM and SIGKILL actions are available directly
    from the UI.

    Config options:
      max_processes  Maximum number of processes to display (default: 20)
      require_sudo   Prefix kill commands with sudo (default: false)
      kill_signal    Default signal for the top-level kill action — TERM or KILL
                     (default: TERM; per-row buttons always offer both)
      cpu_warning    Top-process CPU % that triggers warning (optional)
      cpu_threshold  Top-process CPU % that triggers failed  (optional)
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.max_processes = int(config.get('max_processes', 20))
        self.require_sudo  = bool(config.get('require_sudo', False))
        self.kill_signal   = str(config.get('kill_signal', 'TERM')).upper()
        self.cpu_warning   = float(config['cpu_warning'])   if 'cpu_warning'   in config else None
        self.cpu_threshold = float(config['cpu_threshold'])  if 'cpu_threshold'  in config else None
        self._processes: List[Dict] = []
        self.ssh_collector  = self.internal_modules['collectors']['ssh']
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.db_logger      = self.internal_modules['loggers']['db_logs']
        self.db_metrics     = self.internal_modules['loggers']['db_metrics']

    async def on_collect(self):
        # +1 so head includes the header line plus max_processes data rows
        cmd = (
            f"ps -eo pid,user,pcpu,pmem,comm --sort=-%cpu "
            f"| head -n {self.max_processes + 1}"
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)
        if ret != 0:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        processes = _parse_ps_output(stdout)

        # Fail only when there were data rows (beyond the header) that we couldn't parse
        has_data_rows = len(stdout.strip().splitlines()) > 1
        if not processes and has_data_rows:
            self.db_logger.write(f"Could not parse ps output: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        self._processes = processes
        process_count = len(processes)
        top_cpu = processes[0]['cpu'] if processes else 0.0

        self.db_metrics.metric('process_count', float(process_count))
        self.db_metrics.metric('top_cpu_pct',   top_cpu)

        if self.cpu_warning is not None and self.cpu_threshold is not None:
            overall = _level_for(top_cpu, self.cpu_warning, self.cpu_threshold)
        else:
            overall = 'online'

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        self.db_logger.write(
            f"{process_count} processes, top CPU {top_cpu:.1f}%",
            level=log_level
        )
        self.set_status(overall)

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id != 'kill':
            return False

        pid    = kwargs.get('pid')
        signal = str(kwargs.get('signal', self.kill_signal)).upper()

        if pid is None:
            self.db_logger.write("Kill action missing pid", level="ERROR")
            return False

        prefix = 'sudo ' if self.require_sudo else ''
        ret, _, stderr = await self.ssh_controller.execute_action(
            f"{prefix}kill -{signal} {int(pid)}"
        )
        if ret != 0:
            self.db_logger.write(
                f"Failed to send SIG{signal} to PID {pid}: {stderr}", level="ERROR"
            )
            return False

        self.db_logger.write(f"Sent SIG{signal} to PID {pid}", level="INFO")
        return True

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import asyncio
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('count_card'):
            count_label = info_card('PROCESSES', '--')
        with layout.cell('top_cpu_card'):
            top_cpu_label = info_card('TOP CPU', '-- %')

        columns = [
            {'name': 'pid',     'label': 'PID',     'field': 'pid',     'sortable': True,  'align': 'right'},
            {'name': 'user',    'label': 'USER',    'field': 'user',    'sortable': True,  'align': 'left'},
            {'name': 'cpu',     'label': 'CPU %',   'field': 'cpu',     'sortable': True,  'align': 'right'},
            {'name': 'mem',     'label': 'MEM %',   'field': 'mem',     'sortable': True,  'align': 'right'},
            {'name': 'command', 'label': 'COMMAND', 'field': 'command', 'sortable': True,  'align': 'left'},
            {'name': 'actions', 'label': '',        'field': 'actions', 'sortable': False, 'align': 'center'},
        ]

        with layout.cell('table'):
            table = ui.table(columns=columns, rows=[], row_key='pid').classes('w-full text-sm')
            table.add_slot('body-cell-cpu', '''
                <q-td :props="props">
                    <span :style="{ color: props.row._cpu_color }">{{ props.row.cpu }}</span>
                </q-td>
            ''')
            table.add_slot('body-cell-actions', '''
                <q-td :props="props">
                    <q-btn dense flat icon="cancel" color="warning" size="sm"
                           @click="$parent.$emit('kill_term', props.row)"
                           title="SIGTERM (graceful)" />
                    <q-btn dense flat icon="dangerous" color="negative" size="sm"
                           @click="$parent.$emit('kill_kill', props.row)"
                           title="SIGKILL (force)" />
                </q-td>
            ''')

        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table']()

        async def _do_kill(e, signal):
            pid = (e.args or {}).get('pid')
            if pid is None:
                return
            success = await self.on_action('kill', pid=pid, signal=signal)
            msg = (f'Sent SIG{signal} to PID {pid}' if success
                   else f'Failed to kill PID {pid}')
            ui.notify(msg, type='positive' if success else 'negative')

        table.on('kill_term', lambda e: asyncio.create_task(_do_kill(e, 'TERM')))
        table.on('kill_kill', lambda e: asyncio.create_task(_do_kill(e, 'KILL')))

        def update():
            count_label.text = str(len(self._processes))
            if self._processes:
                top_cpu = self._processes[0]['cpu']
                top_cpu_label.text = f'{top_cpu:.1f}%'
                if self.cpu_warning is not None and self.cpu_threshold is not None:
                    top_cpu_label.style(
                        f'color: {STATUS_COLORS[_level_for(top_cpu, self.cpu_warning, self.cpu_threshold)]}'
                    )
            rows = []
            for p in self._processes:
                cpu_color = STATUS_COLORS['online']
                if self.cpu_warning is not None and self.cpu_threshold is not None:
                    cpu_color = STATUS_COLORS[_level_for(p['cpu'], self.cpu_warning, self.cpu_threshold)]
                rows.append({**p, '_cpu_color': cpu_color})
            table.rows[:] = rows
            table.update()

        ui.timer(5.0, update)
