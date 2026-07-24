from typing import Dict, Any, List

from vigil.collector.plugin_base import CollectorPlugin
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
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.runtime = config.get('runtime', 'docker')
        self.expect_running = set(config.get('expect_running', []) or [])
        self.stopped_warning = bool(config.get('stopped_warning', True))

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(f"{self.runtime} {_PS_FMT} 2>&1")

        combined = f"{stdout}\n{stderr}".lower()
        if ret != 0 and ('command not found' in combined or 'not found' in combined):
            self.db_logger.write(f"{self.runtime} not installed on target", level="WARNING")
            self.set_status('offline')
            return
        if ret != 0:
            self.db_logger.write(f"'{self.runtime} ps' failed: {stderr}", level="ERROR")
            self.set_status('failed')
            return

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
        self.db_metrics.metric('containers_total', float(total))
        self.db_metrics.metric('containers_running', float(len(running)))
        self.db_metrics.metric('containers_stopped', float(len(stopped)))

        if total == 0:
            self.db_logger.write("No containers found", level="INFO")
            self.set_status('online')
            return

        running_set = set(running)
        missing = sorted(self.expect_running - running_set)
        if missing:
            self.db_logger.write(f"Expected containers not running: {', '.join(missing)}", level="ERROR")
            self.set_status('failed')
            self._log_stopped(stopped)
            return

        if self.stopped_warning and stopped:
            self.db_logger.write(
                f"{len(running)} running, {len(stopped)} stopped: {', '.join(stopped)}",
                level="WARNING"
            )
            self.set_status('warning')
            return

        self.db_logger.write(
            f"{len(running)} running, {len(stopped)} stopped, {len(benign)} paused/created",
            level="INFO"
        )
        self.set_status('online')

    def _log_stopped(self, stopped: List[str]):
        if stopped:
            self.db_logger.write(f"Stopped: {', '.join(stopped)}", level="WARNING")

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

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id.startswith('restart:'):
            cname = action_id.split(':', 1)[1]
            if cname not in self.expect_running:
                self.db_logger.write(f"Refusing to restart unlisted container {cname!r}", level="ERROR")
                return False
            status, _, stderr = await self.ssh_controller.execute_action(
                f"{self.runtime} restart {_shquote(cname)}"
            )
            if status != 0:
                self.db_logger.write(f"Restart of {cname} failed: {stderr}", level="ERROR")
            return status == 0
        return False


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
