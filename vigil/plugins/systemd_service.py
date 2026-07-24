import os
import shlex
import time
from typing import Dict, Any, List

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin
from vigil.core.common.time_utils import parse_duration, format_duration, format_age

_DEFAULT_UNIT_FILE_WRITE_PATHS = (
    '/etc/systemd/system',
    '/run/systemd/system',
    '/lib/systemd/system',
    '/usr/lib/systemd/system',
)


_CONTINUOUS_LAYOUT = [
    ['host_card', 'service_card', 'status_card', 'time_card'],
    ['unit_file_card'],
    ['logs'],
]

_ONESHOT_LAYOUT = [
    ['host_card', 'service_card', 'maxage_card', 'state_card'],
    ['unit_file_card'],
    ['history'],
    ['logs'],
]


class SystemdCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.service_name = config.get('service_name')
        self.lines = config.get('lines', 10)
        self.max_age = parse_duration(config['max_age']) if 'max_age' in config else None
        self.allow_unit_file_edit = bool(config.get('allow_unit_file_edit', False))
        self.allowed_write_paths = tuple(config.get('allowed_write_paths', _DEFAULT_UNIT_FILE_WRITE_PATHS))


    async def _collect_journal(self) -> bool:
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
        parts = line.split(' ', 1)
        if len(parts) == 2 and 'T' in parts[0] and parts[0][:4].isdigit():
            return parts[0], line
        return None, line

    async def on_collect(self):
        if self.max_age is not None:
            await self._collect_oneshot()
        else:
            await self._collect_continuous()

    async def _run_systemctl_command(self, command: str) -> bool:
        status, _, stderr = await self.ssh_controller.execute_action(
            f"sudo systemctl {command} {self.service_name if command != 'daemon-reload' else ''}".strip()
        )
        if status != 0:
            self.db_logger.write(f"systemctl {command} failed: {stderr}", level='ERROR')
        return status == 0

    async def _collect_continuous(self):
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
            'echo "result=${result:-empty} exit=${exit_code:-empty} epoch=$epoch active=${active:-unknown} sub=${sub:-unknown}"'
        )
        ret, stdout, stderr = await self.ssh_collector.fetch_output(cmd)

        if ret != 0:
            self.db_logger.write(f"Failed to query service state: {stderr}", level="ERROR")
            self.set_status('failed')
            return

        tokens = dict(tok.split('=', 1) for tok in stdout.strip().split() if '=' in tok)
        result    = tokens.get('result', 'empty')
        exit_code = tokens.get('exit',   'empty')
        active    = tokens.get('active', 'unknown')
        sub       = tokens.get('sub',    'unknown')
        try:
            epoch = int(tokens.get('epoch', '0'))
        except ValueError:
            epoch = 0

        self.db_logger.write(
            f"systemd state: result={result!r} exit_code={exit_code!r} epoch={epoch} active={active!r} sub={sub!r}",
            level="INFO"
        )

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

        await self._collect_journal()


    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Restart Service', 'action_id': 'restart_service', 'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Stop Service',    'action_id': 'stop_service',    'variant': 'danger',  'icon': 'stop'},
            {'name': 'Enable on Boot',  'action_id': 'enable_service', 'variant': 'secondary', 'icon': 'toggle_on'},
            {'name': 'Disable on Boot', 'action_id': 'disable_service','variant': 'secondary', 'icon': 'toggle_off'},
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        if action_id == 'restart_service':
            return await self._run_systemctl_command('restart')

        if action_id == 'stop_service':
            return await self._run_systemctl_command('stop')

        if action_id == 'enable_service':
            return await self._run_systemctl_command('enable')

        if action_id == 'disable_service':
            return await self._run_systemctl_command('disable')

        return False


class SystemdUIPlugin(UIPlugin):
    @property
    def service_name(self):
        return self.config.get('service_name')

    @property
    def max_age(self):
        return parse_duration(self.config['max_age']) if 'max_age' in self.config else None

    @property
    def allow_unit_file_edit(self):
        return bool(self.config.get('allow_unit_file_edit', False))

    @property
    def allowed_write_paths(self):
        return tuple(self.config.get('allowed_write_paths', _DEFAULT_UNIT_FILE_WRITE_PATHS))

    async def _resolve_unit_path(self) -> tuple[bool, str]:
        status, stdout, stderr = await self.ssh_controller.execute_action(
            f"systemctl show -p FragmentPath --value {shlex.quote(self.service_name)}"
        )
        if status != 0:
            return False, stderr.strip() or 'Unable to resolve unit file path'

        path = stdout.strip()
        if not path or path == 'n/a':
            return False, 'Unit file path unavailable'
        return True, path

    def _is_write_path_allowed(self, path: str) -> bool:
        normalized = os.path.abspath(path)
        return any(
            normalized == os.path.abspath(allowed) or
            normalized.startswith(os.path.abspath(allowed) + os.sep)
            for allowed in self.allowed_write_paths
        )

    async def _read_unit_file(self) -> tuple[bool, str]:
        status, stdout, stderr = await self.ssh_controller.execute_action(
            f"sudo systemctl cat {shlex.quote(self.service_name)}"
        )
        if status != 0:
            return False, stderr.strip() or 'Unable to read unit file'
        return True, stdout

    async def _write_unit_file(self, content: str) -> tuple[bool, str]:
        ok, path_or_error = await self._resolve_unit_path()
        if not ok:
            return False, path_or_error

        path = path_or_error
        if not self._is_write_path_allowed(path):
            return False, f'Write path not allowed: {path}'

        cmd = (
            "sudo python3 - <<'PY'\n"
            "from pathlib import Path\n"
            f"Path({shlex.quote(path)}).write_text({shlex.quote(content)}, encoding='utf-8')\n"
            "PY"
        )
        status, _, stderr = await self.ssh_controller.execute_action(cmd)
        if status != 0:
            return False, stderr.strip() or 'Unable to write unit file'
        return True, 'Unit file written successfully'

    async def _run_systemctl_command(self, command: str) -> bool:
        status, _, stderr = await self.ssh_controller.execute_action(
            f"sudo systemctl {command} {self.service_name if command != 'daemon-reload' else ''}".strip()
        )
        return status == 0

    def _render_unit_file_controls(self):
        from nicegui import ui

        with ui.card().classes('p-4 h-full'):
            ui.label('Unit File').classes('font-bold mb-2')
            with ui.row().classes('gap-2 mb-2'):
                ui.button('View Unit File', on_click=self._show_unit_file, color='primary').props('flat')
                ui.button('Reload Daemon', on_click=self._reload_daemon, color='secondary').props('flat')
                if self.allow_unit_file_edit:
                    ui.button('Edit Unit File', on_click=self._edit_unit_file, color='secondary').props('flat')

    async def _show_unit_file(self):
        from nicegui import ui

        ui.spinner().style('display: block')
        ok, content = await self._read_unit_file()
        ui.spinner().delete()
        if not ok:
            ui.notify(content, type='negative')
            return

        with ui.dialog() as dialog:
            ui.dialog_title('Unit File')
            ui.label(self.service_name).classes('text-sm text-slate-500 mb-2')
            ui.textarea(content, readonly=True, auto_grow=True).classes('w-full')
            ui.button('Close', on_click=dialog.close).props('flat')
        dialog.open()

    async def _edit_unit_file(self):
        from nicegui import ui

        ok, content = await self._read_unit_file()
        if not ok:
            ui.notify(content, type='negative')
            return

        with ui.dialog() as dialog:
            ui.dialog_title('Edit Unit File')
            ui.label(self.service_name).classes('text-sm text-slate-500 mb-2')
            editor = ui.textarea(content, auto_grow=True).classes('w-full h-96')
            with ui.row().classes('justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')
                async def save():
                    new_content = editor.value
                    save_ok, message = await self._write_unit_file(new_content)
                    ui.notify(message, type='positive' if save_ok else 'negative')
                    if save_ok:
                        dialog.close()
                ui.button('Save', on_click=save).props('flat primary')
        dialog.open()

    async def _reload_daemon(self):
        from nicegui import ui

        success = await self._run_systemctl_command('daemon-reload')
        ui.notify('Daemon reloaded' if success else 'Daemon reload failed',
                  type='positive' if success else 'negative')


    def render_ui(self, context: str = 'page'):
        if self.max_age is not None:
            self._render_oneshot_ui(context)
        else:
            self._render_continuous_ui(context)

    def _render_continuous_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import StatusHistory
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _CONTINUOUS_LAYOUT if context == 'page' else make_inline_layout(_CONTINUOUS_LAYOUT))
        page = self.page()

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('service_card'):
            info_card('SERVICE', self.service_name)
        with layout.cell('status_card'):
            self.internal_modules['ui']['status_card'](
                page,
                metric_name='active',
                title='SERVICE STATUS',
                on_text='ACTIVE',
                off_text='INACTIVE'
            )
        with layout.cell('time_card'):
            time_label = info_card('LAST COLLECTION', '--:--:--')
        with layout.cell('unit_file_card'):
            self._render_unit_file_controls()
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](page, title='LOGS', limit=100, full_height=True)

        def update_time():
            last = StatusHistory.select().where(
                StatusHistory.collector_id == self.id
            ).order_by(StatusHistory.timestamp.desc()).first()
            if last:
                time_label.text = last.timestamp.strftime('%H:%M:%S')

        page.on_refresh(update_time)
        update_time()
        page.start()

    def _render_oneshot_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.data.database import Metric
        from vigil.web.ui.theme import STATUS_COLORS
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _ONESHOT_LAYOUT if context == 'page' else make_inline_layout(_ONESHOT_LAYOUT))
        page = self.page(metric_names=['is_running', 'last_run_epoch', 'last_run_success'])

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('service_card'):
            info_card('SERVICE', self.service_name)
        with layout.cell('maxage_card'):
            info_card('MAX AGE', format_duration(self.max_age))
        with layout.cell('state_card'):
            state_label = info_card('CURRENT STATE', '--')
        with layout.cell('unit_file_card'):
            self._render_unit_file_controls()
        with layout.cell('history') as history_cell:
            with ui.row().classes('gap-4'):
                result_label = info_card('LAST RESULT', '--')
                age_label = info_card('LAST RUN', '--')
        with layout.cell('logs'):
            self.internal_modules['ui']['logs_table'](page, title='LOGS', limit=100, full_height=True)

        def update():
            def _val(name):
                return page.model.metrics.get(name)

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

        page.on_refresh(update)
        update()
        page.start()
