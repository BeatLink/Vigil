from typing import Dict, Any, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# List every container (running and stopped) as tab-separated Name<TAB>State.
# {{.State}} is "running", "exited", "created", "paused", etc.
_PS_FMT = "ps -a --format '{{.Names}}\t{{.State}}'"

_RUNNING_STATES = {'running', 'up'}
# States that are expected/benign when a container isn't meant to be running.
_BENIGN_STATES = {'created', 'paused'}

_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'running_card', 'stopped_card'],
    ['containers'],
    ['events'],
]


class ContainersCollectorPlugin(CollectorPlugin):
    """
    Monitors Docker or Podman containers over SSH.

    Runs `<runtime> ps -a` and records how many containers are running vs.
    stopped. By default any container in a non-running, non-benign state (e.g.
    "exited", "dead") drives the status to warning; if `expect_running` lists
    specific container names, any of those not running drives status to failed.

    Config options:
      runtime          Container CLI: "docker" or "podman"   (default: "docker")
      expect_running   List of container names that must be running (optional).
                       Any listed name that is missing or not running => failed.
      stopped_warning  If true, any stopped container => warning  (default: true)

    Provides a per-container restart action from the UI.
    """

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
        stopped: List[str] = []   # containers that are neither running nor in a benign state
        benign: List[str] = []    # created/paused — not running, but not alarming either
        for line in stdout.splitlines():
            if '\t' not in line:
                continue
            cname, state = line.split('\t', 1)
            cname, state = cname.strip(), state.strip().lower()
            if not cname:
                continue
            # `docker ps` State is a single word; `podman` may include detail — take the first token.
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

        # Required containers that aren't running are a hard failure.
        running_set = set(running)
        missing = sorted(self.expect_running - running_set)
        if missing:
            self.db_logger.write(f"Expected containers not running: {', '.join(missing)}", level="ERROR")
            self.set_status('failed')
            self._log_stopped(stopped)
            return

        # Any unexpectedly-stopped container is a warning (benign states excluded).
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
        # Restart specific expected containers if configured; otherwise offer a
        # generic "restart all stopped" isn't safe, so only expose named ones.
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
            # Only allow restarting containers the operator declared, to avoid
            # acting on an arbitrary name injected via the action id.
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
    """Dashboard rendering for the containers monitor. get_actions()/on_action()
    are inherited from UIPlugin, which proxies to the collector's live instance."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card
        from vigil.web.ui.theme import STATUS_COLORS

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['containers_total', 'containers_running', 'containers_stopped'])

        def _int_or_dash(v):
            return '--' if v is None else str(int(v))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            info_card('CONTAINERS', '--').bind_text_from(
                page.model, ('metrics', 'containers_total'), backward=_int_or_dash)
        with layout.cell('running_card'):
            info_card('RUNNING', '--').bind_text_from(
                page.model, ('metrics', 'containers_running'), backward=_int_or_dash
            ).style(f"color: {STATUS_COLORS['online']}")
        with layout.cell('stopped_card'):
            stopped_label = info_card('STOPPED', '--').bind_text_from(
                page.model, ('metrics', 'containers_stopped'), backward=_int_or_dash)
        with layout.cell('containers'):
            ui.element('div')  # reserved for future per-container detail
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            stopped = page.model.metrics.get('containers_stopped')
            if stopped is not None:
                color = STATUS_COLORS['warning'] if stopped else STATUS_COLORS['online']
                stopped_label.style(f"color: {color}")

        page.on_refresh(update_color)
        page.start()


def _shquote(s: str) -> str:
    """Single-quote a string for safe embedding inside a shell command."""
    return "'" + s.replace("'", "'\\''") + "'"
