from typing import Dict, Any, List, Optional, Union

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.plugin_utils import level_for as _level_for

_SEVERITY = {'online': 0, 'warning': 1, 'failed': 2}


def _parse_ps_output(stdout: str) -> List[Dict]:
    processes = []
    lines = stdout.strip().splitlines()
    if len(lines) < 2:
        return processes
    for line in lines[1:]:
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


_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'top_cpu_card'],
    ['table'],
    ['events'],
]


class ProcessesCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.max_processes = int(config.get('max_processes', 20))
        self.require_sudo  = bool(config.get('require_sudo', False))
        self.kill_signal   = str(config.get('kill_signal', 'TERM')).upper()
        self.cpu_warning   = float(config['cpu_warning'])   if 'cpu_warning'   in config else None
        self.cpu_threshold = float(config['cpu_threshold'])  if 'cpu_threshold'  in config else None

    def commands(self) -> List[Command]:
        cmd = (
            f"ps -eo pid,user,pcpu,pmem,comm --sort=-%cpu "
            f"| head -n {self.max_processes + 1}"
        )
        return [Command(cmd)]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr
        if ret != 0:
            return CollectResult.failed(f"Collection failed: {stderr}")

        processes = _parse_ps_output(stdout)

        has_data_rows = len(stdout.strip().splitlines()) > 1
        if not processes and has_data_rows:
            return CollectResult.failed(f"Could not parse ps output: {stdout!r}")

        process_count = len(processes)
        top_cpu = processes[0]['cpu'] if processes else 0.0

        if self.cpu_warning is not None and self.cpu_threshold is not None:
            overall = _level_for(top_cpu, self.cpu_warning, self.cpu_threshold)
        else:
            overall = 'online'

        log_level = "ERROR" if overall == 'failed' else "WARNING" if overall == 'warning' else "INFO"
        return CollectResult(
            metrics={'process_count': float(process_count), 'top_cpu_pct': top_cpu},
            snapshot=processes,
            logs=[(f"{process_count} processes, top CPU {top_cpu:.1f}%", log_level)],
            status=overall,
        )

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        if action_id != 'kill':
            return None

        pid    = kwargs.get('pid')
        signal = str(kwargs.get('signal', self.kill_signal)).upper()

        if pid is None:
            return CollectResult.failed("Kill action missing pid")

        prefix = 'sudo ' if self.require_sudo else ''
        return ActionPlan(f"{prefix}kill -{signal} {int(pid)}")

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id != 'kill':
            return result.exit_code == 0
        pid = kwargs.get('pid')
        signal = str(kwargs.get('signal', self.kill_signal)).upper()
        if result.exit_code != 0:
            return CollectResult.failed(f"Failed to send SIG{signal} to PID {pid}: {result.stderr}")
        return CollectResult(logs=[(f"Sent SIG{signal} to PID {pid}", "INFO")], success=True)


class ProcessesUIPlugin(UIPlugin):
    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        import asyncio
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        cpu_warning   = float(self.config['cpu_warning'])   if 'cpu_warning'   in self.config else None
        cpu_threshold = float(self.config['cpu_threshold']) if 'cpu_threshold' in self.config else None

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.ui.page()

        with layout.cell('host_card'):
            self.ui.host_card()
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

        with layout.cell('events'):
            self.ui.events_table(page)

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
            count_metric = self.storage.latest_metric('process_count')
            top_cpu_metric = self.storage.latest_metric('top_cpu_pct')
            if count_metric is not None:
                count_label.text = str(int(count_metric.value))
            if top_cpu_metric is not None:
                top_cpu = top_cpu_metric.value
                top_cpu_label.text = f'{top_cpu:.1f}%'
                if cpu_warning is not None and cpu_threshold is not None:
                    top_cpu_label.style(
                        f'color: {STATUS_COLORS[_level_for(top_cpu, cpu_warning, cpu_threshold)]}'
                    )
            rows = []
            for p in self.storage.latest_snapshot(default=[]):
                cpu_color = STATUS_COLORS['online']
                if cpu_warning is not None and cpu_threshold is not None:
                    cpu_color = STATUS_COLORS[_level_for(p['cpu'], cpu_warning, cpu_threshold)]
                rows.append({**p, '_cpu_color': cpu_color})
            table.rows[:] = rows
            table.update()

        page.on_refresh(update)
        update()
        page.start()
