from typing import Dict, Any, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

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
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.uri = config.get('uri', 'qemu:///system')
        self.expect_running = set(config.get('expect_running', []) or [])
        self.offline_warning = bool(config.get('offline_warning', True))

    def _virsh(self, subcmd: str) -> str:
        return f"virsh -c {_shquote(self.uri)} {subcmd}"

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(f"{self._virsh('list --all')} 2>&1")

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined):
            self.db_logger.write("virsh not installed on target", level="WARNING")
            self.set_status('offline')
            return
        if ret != 0 and 'failed to connect' in combined:
            self.db_logger.write(f"libvirt not reachable: {stderr}", level="ERROR")
            self.set_status('failed')
            return
        if ret != 0:
            self.db_logger.write(f"virsh list failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

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
        self.db_metrics.metric('vms_total', float(total))
        self.db_metrics.metric('vms_running', float(len(running)))
        self.db_metrics.metric('vms_stopped', float(len(stopped) + len(benign)))

        if total == 0:
            self.db_logger.write("No VMs defined", level="INFO")
            self.set_status('online')
            return

        running_set = set(running)
        missing = sorted(self.expect_running - running_set)
        if missing:
            self.db_logger.write(f"Expected VMs not running: {', '.join(missing)}", level="ERROR")
            self.set_status('failed')
            return

        if self.offline_warning and stopped:
            self.db_logger.write(
                f"{len(running)} running, {len(stopped)} in error state: {', '.join(stopped)}",
                level="WARNING"
            )
            self.set_status('warning')
            return

        self.db_logger.write(
            f"{len(running)} running, {len(benign)} shut off", level="INFO"
        )
        self.set_status('online')

    def get_actions(self) -> List[Dict[str, str]]:
        actions = []
        for name in sorted(self.expect_running):
            actions.append({'name': f'Start {name}', 'action_id': f'start:{name}',
                            'variant': 'primary', 'icon': 'play_arrow'})
            actions.append({'name': f'Shutdown {name}', 'action_id': f'shutdown:{name}',
                            'variant': 'danger', 'icon': 'stop'})
        return actions

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if ':' not in action_id:
            return False
        verb, name = action_id.split(':', 1)
        if name not in self.expect_running:
            self.db_logger.write(f"Refusing to {verb} unlisted VM {name!r}", level="ERROR")
            return False
        if verb == 'start':
            subcmd = f"start {_shquote(name)}"
        elif verb == 'shutdown':
            subcmd = f"shutdown {_shquote(name)}"
        else:
            return False
        status, _, stderr = await self.ssh_controller.execute_action(self._virsh(subcmd))
        if status != 0:
            self.db_logger.write(f"{verb} of {name} failed: {stderr}", level="ERROR")
        return status == 0


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
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('vms_always_online')
def _vms_running_color(v):
    return None if v is None else 'online'
