import time
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.components import info_card


def _fmt_duration(seconds: int) -> str:
    """Formats a number of seconds as a compact human-readable duration."""
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        h, m = seconds // 3600, (seconds % 3600) // 60
        return f'{h}h {m}m' if m else f'{h}h'
    d, h = seconds // 86400, (seconds % 86400) // 3600
    return f'{d}d {h}h' if h else f'{d}d'


def _fmt_age(seconds: int) -> str:
    """Formats how long ago something happened."""
    return 'Never' if seconds < 0 else f'{_fmt_duration(seconds)} ago'


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
        self.max_age = config.get('max_age')  # None → continuous, int → oneshot

        self.ssh_collector = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

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

        l_ret, stdout, stderr = await self.ssh_collector.fetch_output(
            f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        )
        if l_ret == 0:
            for line in stdout.splitlines():
                lvl = 'ERROR' if any(k in line.upper() for k in ('ERROR', 'FAIL', 'CRITICAL')) else 'INFO'
                self.db_logger.write(line, level=lvl)
            self.set_status('online' if is_active else 'warning')
        else:
            self.db_logger.write(f"Log collection failed: {stderr}", level="ERROR")
            self.set_status('failed')

    async def _collect_oneshot(self):
        """
        Oneshot check: was the last run successful and recent enough?
        Uses a single SSH call to get both the Result and the completion
        timestamp, converting the timestamp to a Unix epoch on the remote side
        to avoid locale-dependent date parsing.
        """
        cmd = (
            f"result=$(systemctl show {self.service_name} -p Result --value); "
            f"ts=$(systemctl show {self.service_name} -p InactiveEnterTimestamp --value); "
            'if [ -n "$ts" ] && [ "$ts" != "n/a" ]; then '
            '  epoch=$(date -d "$ts" +%s 2>/dev/null || echo 0); '
            'else epoch=0; fi; '
            'echo "$result $epoch"'
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)

        if ret != 0:
            self.db_logger.write(f"Failed to query service state: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        parts = stdout.strip().split()
        if len(parts) < 2:
            self.db_logger.write(f"Unexpected output from service query: {stdout!r}", level="ERROR")
            self.set_status('failed')
            return

        result = parts[0]
        try:
            epoch = int(parts[1])
        except ValueError:
            epoch = 0

        age = (int(time.time()) - epoch) if epoch > 0 else -1

        self.db_metrics.metric('last_run_epoch', float(epoch))
        self.db_metrics.metric('last_run_success', 1.0 if result == 'success' else 0.0)

        if epoch == 0:
            self.db_logger.write("Service has never run", level="WARNING")
            self.set_status('failed')
        elif result != 'success':
            self.db_logger.write(f"Last run failed (result: {result})", level="ERROR")
            self.set_status('failed')
        elif age > self.max_age:
            self.db_logger.write(
                f"Last run was {_fmt_age(age)}, exceeds max_age of {_fmt_duration(self.max_age)}",
                level="WARNING"
            )
            self.set_status('failed')
        else:
            self.db_logger.write(f"Last run {_fmt_age(age)}, result: {result}", level="INFO")
            self.set_status('online')

        # Fetch recent logs regardless of result
        l_ret, log_out, _ = await self.ssh_collector.fetch_output(
            f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        )
        if l_ret == 0:
            for line in log_out.splitlines():
                lvl = 'ERROR' if any(k in line.upper() for k in ('ERROR', 'FAIL', 'CRITICAL')) else 'INFO'
                self.db_logger.write(line, level=lvl)

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def render_ui(self):
        if self.max_age is not None:
            self._render_oneshot_ui()
        else:
            self._render_continuous_ui()

    def _render_continuous_ui(self):
        from nicegui import ui
        from vigil.core.data.database import StatusHistory

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()
            info_card('SERVICE', self.service_name)
            self.internal_modules['ui']['status_card'](
                metric_name='active',
                title='SERVICE STATUS',
                on_text='ACTIVE',
                off_text='INACTIVE'
            )
            time_label = info_card('LAST COLLECTION', '--:--:--')

            def update_time():
                last = StatusHistory.select().where(
                    StatusHistory.collector_id == self.id
                ).order_by(StatusHistory.timestamp.desc()).first()
                if last:
                    time_label.text = last.timestamp.strftime('%H:%M:%S')
            ui.timer(2.0, update_time)

        self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

    def _render_oneshot_ui(self):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.core.ui.theme import STATUS_COLORS

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()
            info_card('SERVICE', self.service_name)
            info_card('MAX AGE', _fmt_duration(self.max_age))

            result_label = info_card('LAST RESULT', '--')
            age_label = info_card('LAST RUN', '--')

            def update():
                def latest(metric):
                    m = Metric.select().where(
                        (Metric.collector == self.name) & (Metric.metric_name == metric)
                    ).order_by(Metric.timestamp.desc()).first()
                    return m.value if m else None

                epoch_val = latest('last_run_epoch')
                success_val = latest('last_run_success')

                if epoch_val is None:
                    return

                is_ok = success_val is not None and success_val > 0.5
                result_label.text = 'SUCCESS' if is_ok else 'FAILED'
                result_label.style(f"color: {STATUS_COLORS['online' if is_ok else 'failed']}")

                epoch = int(epoch_val)
                if epoch == 0:
                    age_label.text = 'Never run'
                    age_label.style(f"color: {STATUS_COLORS['failed']}")
                else:
                    age = int(time.time()) - epoch
                    age_label.text = _fmt_age(age)
                    color = STATUS_COLORS['failed'] if age > self.max_age else STATUS_COLORS['online']
                    age_label.style(f"color: {color}")

            ui.timer(5.0, update)

        self.internal_modules['ui']['logs_table'](title='LOGS', limit=100, full_height=True)

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
