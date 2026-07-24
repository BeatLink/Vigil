from typing import Dict, Any, List, Optional, Union

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

_PS_FMT = "ps -a --format '{{.Names}}\t{{.State}}'"

_RUNNING_STATES = {'running', 'up'}
_BENIGN_STATES = {'created', 'paused'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'running_card', 'stopped_card'],
    ['containers'],
    ['events'],
]


class ContainersCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.runtime = config.get('runtime', 'docker')
        self.expect_running = set(config.get('expect_running', []) or [])
        self.stopped_warning = bool(config.get('stopped_warning', True))

    def commands(self) -> List[Command]:
        return [Command(f"{self.runtime} {_PS_FMT} 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined):
            return CollectResult.failed(f"{self.runtime} not installed on target",
                                        level="WARNING", status='offline')
        if ret != 0:
            return CollectResult.failed(f"'{self.runtime} ps' failed: {stderr}")

        running: List[str] = []
        stopped: List[str] = []
        benign: List[str] = []
        for line in stdout.splitlines():
            if '\t' not in line:
                continue
            cname, state = line.split('\t', 1)
            cname, state = cname.strip(), state.strip().lower()
            if not cname:
                continue
            state_word = state.split()[0] if state else ''
            if state_word in _RUNNING_STATES:
                running.append(cname)
            elif state_word in _BENIGN_STATES:
                benign.append(cname)
            else:
                stopped.append(cname)

        total = len(running) + len(stopped) + len(benign)
        metrics = {
            'containers_total': float(total),
            'containers_running': float(len(running)),
            'containers_stopped': float(len(stopped)),
        }

        if total == 0:
            return CollectResult(metrics=metrics, logs=[("No containers found", "INFO")], status='online')

        running_set = set(running)
        missing = sorted(self.expect_running - running_set)
        if missing:
            logs = [(f"Expected containers not running: {', '.join(missing)}", "ERROR")]
            if stopped:
                logs.append((f"Stopped: {', '.join(stopped)}", "WARNING"))
            return CollectResult(metrics=metrics, logs=logs, status='failed')

        if self.stopped_warning and stopped:
            return CollectResult(
                metrics=metrics,
                logs=[(f"{len(running)} running, {len(stopped)} stopped: {', '.join(stopped)}", "WARNING")],
                status='warning',
            )

        return CollectResult(
            metrics=metrics,
            logs=[(f"{len(running)} running, {len(stopped)} stopped, {len(benign)} paused/created", "INFO")],
            status='online',
        )

    def get_actions(self) -> List[Dict[str, str]]:
        actions = []
        for cname in sorted(self.expect_running):
            actions.append({
                'name': f'Restart {cname}',
                'action_id': f'restart:{cname}',
                'variant': 'primary',
                'icon': 'restart_alt',
            })
        return actions

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        if action_id.startswith('restart:'):
            cname = action_id.split(':', 1)[1]
            if cname not in self.expect_running:
                return CollectResult.failed(f"Refusing to restart unlisted container {cname!r}")
            return ActionPlan(f"{self.runtime} restart {_shquote(cname)}")
        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id.startswith('restart:') and result.exit_code != 0:
            cname = action_id.split(':', 1)[1]
            return CollectResult.failed(f"Restart of {cname} failed: {result.stderr}")
        return result.exit_code == 0


class ContainersUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'containers_total', 'title': 'CONTAINERS', 'format': 'int'},
            'running_card': {
                'metric': 'containers_running', 'title': 'RUNNING', 'format': 'int',
                'color': 'containers_always_online',
            },
            'stopped_card': {
                'metric': 'containers_stopped', 'title': 'STOPPED', 'format': 'int',
                'color': 'nonzero_warning',
            },
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('containers_always_online')
def _running_color(v):
    return None if v is None else 'online'


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
