import time
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration, format_duration, format_age
from vigil.core.ui.components import info_card, safe_timer


_CONTINUOUS_LAYOUT = [
    ['host_card', 'service_card', 'status_card', 'time_card'],
    ['logs'],
]

_ONESHOT_LAYOUT = [
    ['host_card', 'service_card', 'maxage_card', 'state_card'],
    ['history'],
    ['logs'],
]


class SystemdPlugin(BasePlugin):
    """
    Monitors systemd services over SSH.

    Two modes selected by whether `max_age` is set in config:

    Continuous mode (default): checks `systemctl is-active` each cycle.
      Suitable for long-running daemons (nginx, unbound, etc.).

    Oneshot mode (max_age set): checks the result and timestamp of the last
      completed run via `systemctl show`. Reports failed if the last run did
      not succeed or completed more than `max_age` seconds ago.
      Suitable for timer-driven services (nixos-upgrade, backup jobs, etc.).
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.service_name = config.get('service_name')
        self.lines = config.get('lines', 10)
        self.max_age = parse_duration(config['max_age']) if 'max_age' in config else None


    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

    async def _collect_journal(self) -> bool:
        """
        Fetch the last `lines` journald entries for the unit and persist them
        with per-line deduplication.

        Uses `--output=short-iso` so each line is prefixed with a stable,
        sortable timestamp. That timestamp anchors the dedup identity, so the
        same line re-appearing on the next cycle is stored only once. Returns
        True on a successful fetch, False on error (caller decides status).
        """
        ret, stdout, stderr = await self.ssh_collector.fetch_output(
            f"journalctl -u {self.service_name} -n {self.lines} "
            f"--no-pager --output=short-iso"
        )
        if ret != 0:
            self.db_logger.write(f"Log collection failed: {stderr}", level="ERROR")
            return False

        for line in stdout.splitlines():
            if not line.strip():
                continue
            log_time, message = self._split_iso_line(line)
            level = 'ERROR' if any(k in line.upper() for k in ('ERROR', 'FAIL', 'CRITICAL')) else 'INFO'
            self.db_logger.log_line(message, level=level, log_time=log_time)
        return True

    @staticmethod
    def _split_iso_line(line: str):
        """
        Split a `short-iso` journald line into (timestamp, message).

        Format: '2024-05-01T12:00:00+0000 host unit[pid]: message'. The first
        three whitespace-separated tokens are the ISO timestamp, host, and unit
        prefix; we keep the timestamp for dedup and store the full line as the
        message so nothing is lost. Falls back gracefully on unexpected shapes.
        """
        parts = line.split(' ', 1)
        if len(parts) == 2 and 'T' in parts[0] and parts[0][:4].isdigit():
            return parts[0], line
        return None, line

    async def on_collect(self):
        if self.max_age is not None:
            await self._collect_oneshot()
        else:
            await self._collect_continuous()

    async def _collect_continuous(self):
        """Standard check: is the service currently active?"""
        s_ret, s_out, _ = await self.ssh_collector.fetch_output(
            f"systemctl is-active {self.service_name}"
        )
        is_active = s_ret == 0 and s_out.strip() == 'active'
        self.db_metrics.metric('active', 1.0 if is_active else 0.0)

        if await self._collect_journal():
            self.set_status('online' if is_active else 'warning')
        else:
            self.set_status('failed')

    async def _collect_oneshot(self):
        """
        Oneshot check: was the last run successful and recent enough?

        Uses ExecMainExitTimestamp as the primary completion time — this is set
        whenever the main process exits, including for RemainAfterExit=yes services
        that stay 'active' after the command finishes (e.g. nixos-upgrade.service).
        InactiveEnterTimestamp is used as a fallback for services that do go inactive.

        Considers a run successful if Result=success OR ExecMainStatus=0, because
        some oneshot scripts exit with result=exit-code even on clean completion.
        """
        cmd = (
            f"result=$(systemctl show {self.service_name} -p Result --value); "
            f"exit_code=$(systemctl show {self.service_name} -p ExecMainStatus --value); "
            f"active=$(systemctl show {self.service_name} -p ActiveState --value); "
            f"sub=$(systemctl show {self.service_name} -p SubState --value); "
            f"ts=$(systemctl show {self.service_name} -p ExecMainExitTimestamp --value); "
            '[ -z "$ts" ] || [ "$ts" = "n/a" ] && '
            f"ts=$(systemctl show {self.service_name} -p InactiveEnterTimestamp --value); "
            'if [ -n "$ts" ] && [ "$ts" != "n/a" ]; then '
            '  epoch=$(date -d "$ts" +%s 2>/dev/null || echo 0); '
            'else epoch=0; fi; '
            # Use placeholder tokens so empty values never collapse the field count
            'echo "result=${result:-empty} exit=${exit_code:-empty} epoch=$epoch active=${active:-unknown} sub=${sub:-unknown}"'
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)

        if ret != 0:
            self.db_logger.write(f"Failed to query service state: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        # Parse key=value tokens — immune to empty fields collapsing word count
        tokens = dict(tok.split('=', 1) for tok in stdout.strip().split() if '=' in tok)
        result    = tokens.get('result', 'empty')
        exit_code = tokens.get('exit',   'empty')
        active    = tokens.get('active', 'unknown')
        sub       = tokens.get('sub',    'unknown')
        try:
            epoch = int(tokens.get('epoch', '0'))
        except ValueError:
            epoch = 0

        # Log raw values so failures are diagnosable from the dashboard
        self.db_logger.write(
            f"systemd state: result={result!r} exit_code={exit_code!r} epoch={epoch} active={active!r} sub={sub!r}",
            level="INFO"
        )

        # Service is "currently running" while activating or actively executing its main process
        _RUNNING_SUBSTATES = {'running', 'start', 'start-pre', 'start-post', 'start-chroot', 'reload'}
        is_running = active == 'activating' or (active == 'active' and sub in _RUNNING_SUBSTATES)

        is_success = result == 'success' or exit_code == '0'
        age = (int(time.time()) - epoch) if epoch > 0 else -1

        self.db_metrics.metric('last_run_epoch', float(epoch))
        self.db_metrics.metric('last_run_success', 1.0 if is_success else 0.0)
        self.db_metrics.metric('is_running', 1.0 if is_running else 0.0)

        if is_running:
            self.db_logger.write("Service is currently running", level="INFO")
            self.set_status('online')
        elif epoch == 0:
            self.db_logger.write("Service has never run", level="WARNING")
            self.set_status('failed')
        elif not is_success:
            self.db_logger.write(f"Last run failed (result: {result}, exit: {exit_code})", level="ERROR")
            self.set_status('failed')
        elif age > self.max_age:
            self.db_logger.write(
                f"Last run was {format_age(age)}, exceeds max_age of {format_duration(self.max_age)}",
                level="WARNING"
            )
            self.set_status('failed')
        else:
            self.db_logger.write(f"Last run {format_age(age)}, result: {result}", level="INFO")
            self.set_status('online')

        # Fetch recent logs regardless of result (deduplicated + persisted)
        await self._collect_journal()

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def render_ui(self, context: str = 'page'):
        if self.max_age is not None:
            self._render_oneshot_ui(context)
        else:
            self._render_continuous_ui(context)

    def _render_continuous_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import StatusHistory
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _CONTINUOUS_LAYOUT if context == 'page' else make_inline_layout(_CONTINUOUS_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('service_card'):
            info_card('SERVICE', self.service_name)
        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                metric_name='active',
                title='SERVICE STATUS',
                on_text='ACTIVE',
                off_text='INACTIVE'
            )
        with layout.cell('time_card'):
            time_label = info_card('LAST COLLECTION', '--:--:--')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

        def update_time():
            last = StatusHistory.select().where(
                StatusHistory.collector_id == self.id
            ).order_by(StatusHistory.timestamp.desc()).first()
            if last:
                time_label.text = last.timestamp.strftime('%H:%M:%S')

        safe_timer(2.0, update_time)

    def _render_oneshot_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.theme import STATUS_COLORS
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _ONESHOT_LAYOUT if context == 'page' else make_inline_layout(_ONESHOT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('service_card'):
            info_card('SERVICE', self.service_name)
        with layout.cell('maxage_card'):
            info_card('MAX AGE', format_duration(self.max_age))
        with layout.cell('state_card'):
            state_label = info_card('CURRENT STATE', '--')
        with layout.cell('history') as history_cell:
            with ui.row().classes('gap-4'):
                result_label = info_card('LAST RESULT', '--')
                age_label = info_card('LAST RUN', '--')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

        def update():
            def _val(name):
                m = self.latest_metric(name)
                return m.value if m else None

            run_val     = _val('is_running')
            epoch_val   = _val('last_run_epoch')
            success_val = _val('last_run_success')

            is_now_running = run_val is not None and run_val > 0.5

            if is_now_running:
                state_label.text = 'RUNNING'
                state_label.style(f"color: {STATUS_COLORS['warning']}")
                history_cell.set_visibility(False)
            else:
                history_cell.set_visibility(True)

                if epoch_val is None:
                    state_label.text = 'UNKNOWN'
                    state_label.style(f"color: {STATUS_COLORS['offline']}")
                    return

                is_ok = success_val is not None and success_val > 0.5
                epoch = int(epoch_val)

                if epoch == 0:
                    state_label.text = 'NEVER RUN'
                    state_label.style(f"color: {STATUS_COLORS['failed']}")
                else:
                    state_label.text = 'IDLE'
                    state_label.style(f"color: {STATUS_COLORS['online']}")

                result_label.text = 'SUCCESS' if is_ok else 'FAILED'
                result_label.style(f"color: {STATUS_COLORS['online' if is_ok else 'failed']}")

                if epoch == 0:
                    age_label.text = 'Never run'
                    age_label.style(f"color: {STATUS_COLORS['failed']}")
                else:
                    age = int(time.time()) - epoch
                    age_label.text = format_age(age)
                    color = STATUS_COLORS['failed'] if age > self.max_age else STATUS_COLORS['online']
                    age_label.style(f"color: {color}")

        update()
        safe_timer(5.0, update)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Restart Service', 'action_id': 'restart_service', 'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Stop Service',    'action_id': 'stop_service',    'variant': 'danger',  'icon': 'stop'},
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id == 'restart_service':
            status, _, stderr = await self.ssh_controller.execute_action(
                f"sudo systemctl restart {self.service_name}"
            )
            if status != 0:
                self.db_logger.write(f"Restart failed: {stderr}", level="ERROR")
            return status == 0

        if action_id == 'stop_service':
            status, _, stderr = await self.ssh_controller.execute_action(
                f"sudo systemctl stop {self.service_name}"
            )
            if status != 0:
                self.db_logger.write(f"Stop failed: {stderr}", level="ERROR")
            return status == 0

        return False
