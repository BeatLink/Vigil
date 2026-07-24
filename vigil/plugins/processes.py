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
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self.cpu_warning   = float(config['cpu_warning'])   if 'cpu_warning'   in config else None
        self.cpu_threshold = float(config['cpu_threshold']) if 'cpu_threshold' in config else None

        if self.cpu_warning is not None and self.cpu_threshold is not None:
            from vigil.web.ui.spec import register_item_color_rule, register_color_rule, threshold_color
            self._cpu_color_rule_name = f'processes_cpu_{self.id}'
            register_item_color_rule(self._cpu_color_rule_name)(
                lambda row: _level_for(row['cpu'], self.cpu_warning, self.cpu_threshold)
            )
            self._top_cpu_color_rule_name = f'processes_top_cpu_{self.id}'
            register_color_rule(self._top_cpu_color_rule_name)(
                threshold_color(warning=self.cpu_warning, threshold=self.cpu_threshold))
        else:
            self._cpu_color_rule_name = None
            self._top_cpu_color_rule_name = None

    @property
    def UI_SPEC(self):
        return {
            'layout': _DEFAULT_LAYOUT,
            'cards': {
                'count_card': {'metric': 'process_count', 'title': 'PROCESSES', 'format': 'int'},
                'top_cpu_card': {
                    'metric': 'top_cpu_pct', 'title': 'TOP CPU', 'format': 'percent1',
                    **({'color': self._top_cpu_color_rule_name} if self._top_cpu_color_rule_name else {}),
                },
            },
            'tables': {
                'table': {
                    'row_key': 'pid',
                    'columns': [
                        {'name': 'pid', 'label': 'PID', 'field': 'pid', 'sortable': True, 'align': 'right'},
                        {'name': 'user', 'label': 'USER', 'field': 'user', 'sortable': True, 'align': 'left'},
                        {'name': 'cpu', 'label': 'CPU %', 'field': 'cpu', 'sortable': True, 'align': 'right',
                         **({'cell_color_by': self._cpu_color_rule_name} if self._cpu_color_rule_name else {})},
                        {'name': 'mem', 'label': 'MEM %', 'field': 'mem', 'sortable': True, 'align': 'right'},
                        {'name': 'command', 'label': 'COMMAND', 'field': 'command', 'sortable': True, 'align': 'left'},
                    ],
                    'row_actions': [
                        {'id': 'kill_term', 'icon': 'cancel', 'color': 'warning',
                         'tooltip': 'SIGTERM (graceful)', 'kind': 'dispatch', 'action_id': 'kill',
                         'params': {'pid': 'pid'}, 'static_params': {'signal': 'TERM'}},
                        {'id': 'kill_kill', 'icon': 'dangerous', 'color': 'negative',
                         'tooltip': 'SIGKILL (force)', 'kind': 'dispatch', 'action_id': 'kill',
                         'params': {'pid': 'pid'}, 'static_params': {'signal': 'KILL'}},
                    ],
                },
            },
            'events': True,
        }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)
