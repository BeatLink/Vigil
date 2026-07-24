from typing import Dict, Any, List, Optional, Union

from vigil.plugins.base.collector_plugin_base import CollectorPlugin
from vigil.core.connectors.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.plugins.base.web_plugin_base import UIPlugin

_LIST_CMD = "virsh list --all"

_RUNNING_STATES = {'running'}
_BENIGN_STATES = {'shut off', 'shutoff'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'running_card', 'stopped_card'],
    ['vms'],
    ['events'],
]


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_row(line: str):
    stripped = line.strip()
    if not stripped or stripped.startswith('---') or set(stripped) <= set('- '):
        return None, None
    parts = line.split()
    if parts[:2] == ['Id', 'Name'] or (parts and parts[0] == 'Id'):
        return None, None
    if len(parts) < 3:
        return None, None
    name = parts[1]
    state = ' '.join(parts[2:]).lower()
    return name, state


class VmsCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.uri = config.get('uri', 'qemu:///system')
        self.expect_running = set(config.get('expect_running', []) or [])
        self.offline_warning = bool(config.get('offline_warning', True))

    def _virsh(self, subcmd: str) -> str:
        return f"virsh -c {_shquote(self.uri)} {subcmd}"

    def commands(self) -> List[Command]:
        return [Command(f"{self._virsh('list --all')} 2>&1")]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        ret, stdout, stderr = results[0].exit_code, results[0].stdout, results[0].stderr

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined):
            return CollectResult.failed("virsh not installed on target", level="WARNING", status='offline')
        if ret != 0 and 'failed to connect' in combined:
            return CollectResult.failed(f"libvirt not reachable: {stderr}")
        if ret != 0:
            return CollectResult.failed(f"virsh list failed: {stderr}")

        running: List[str] = []
        stopped: List[str] = []
        benign: List[str] = []
        for line in stdout.splitlines():
            name, state = _parse_row(line)
            if name is None:
                continue
            if state in _RUNNING_STATES:
                running.append(name)
            elif state in _BENIGN_STATES:
                benign.append(name)
            else:
                stopped.append(name)

        total = len(running) + len(stopped) + len(benign)
        metrics = {
            'vms_total': float(total),
            'vms_running': float(len(running)),
            'vms_stopped': float(len(stopped) + len(benign)),
        }

        if total == 0:
            return CollectResult(metrics=metrics, logs=[("No VMs defined", "INFO")], status='online')

        running_set = set(running)
        missing = sorted(self.expect_running - running_set)
        if missing:
            return CollectResult(
                metrics=metrics,
                logs=[(f"Expected VMs not running: {', '.join(missing)}", "ERROR")],
                status='failed',
            )

        if self.offline_warning and stopped:
            return CollectResult(
                metrics=metrics,
                logs=[(f"{len(running)} running, {len(stopped)} in error state: {', '.join(stopped)}", "WARNING")],
                status='warning',
            )

        return CollectResult(
            metrics=metrics,
            logs=[(f"{len(running)} running, {len(benign)} shut off", "INFO")],
            status='online',
        )

    def get_actions(self) -> List[Dict[str, str]]:
        actions = []
        for name in sorted(self.expect_running):
            actions.append({'name': f'Start {name}', 'action_id': f'start:{name}',
                            'variant': 'primary', 'icon': 'play_arrow'})
            actions.append({'name': f'Shutdown {name}', 'action_id': f'shutdown:{name}',
                            'variant': 'danger', 'icon': 'stop'})
        return actions

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        if ':' not in action_id:
            return None
        verb, name = action_id.split(':', 1)
        if name not in self.expect_running:
            return CollectResult.failed(f"Refusing to {verb} unlisted VM {name!r}")
        if verb == 'start':
            subcmd = f"start {_shquote(name)}"
        elif verb == 'shutdown':
            subcmd = f"shutdown {_shquote(name)}"
        else:
            return None
        return ActionPlan(self._virsh(subcmd))

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if result.exit_code != 0:
            verb, name = action_id.split(':', 1)
            return CollectResult.failed(f"{verb} of {name} failed: {result.stderr}")
        return True


class VmsUIPlugin(UIPlugin):
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'vms_total', 'title': 'VMS', 'format': 'int'},
            'running_card': {
                'metric': 'vms_running', 'title': 'RUNNING', 'format': 'int',
                'color': 'vms_always_online',
            },
            'stopped_card': {'metric': 'vms_stopped', 'title': 'STOPPED', 'format': 'int'},
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.core.ui.ui.spec import generic_render
        generic_render(self, context)


from vigil.core.ui.ui.spec import register_color_rule


@register_color_rule('vms_always_online')
def _vms_running_color(v):
    return None if v is None else 'online'
