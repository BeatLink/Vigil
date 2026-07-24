import os
import shlex
import time
from typing import Any, Dict, List, Optional, Union

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import ActionPlan, CmdResult, Command, CollectResult
from vigil.web.web_plugin_base import UIPlugin
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

_RUNNING_SUBSTATES = {'running', 'start', 'start-pre', 'start-post', 'start-chroot', 'reload'}


class SystemdCollectorPlugin(CollectorPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self.service_name = config.get('service_name')
        self.lines = config.get('lines', 10)
        self.max_age = parse_duration(config['max_age']) if 'max_age' in config else None
        self.allow_unit_file_edit = bool(config.get('allow_unit_file_edit', False))
        self.allowed_write_paths = tuple(config.get('allowed_write_paths', _DEFAULT_UNIT_FILE_WRITE_PATHS))

    def commands(self) -> List[Command]:
        journal_cmd = Command(
            f"journalctl -u {self.service_name} -n {self.lines} "
            f"--no-pager --output=short-iso"
        )
        if self.max_age is not None:
            return [Command(self._oneshot_state_cmd()), journal_cmd]
        return [Command(f"systemctl is-active {self.service_name}"), journal_cmd]

    def parse(self, results: List[CmdResult]) -> CollectResult:
        if self.max_age is not None:
            return self._parse_oneshot(results)
        return self._parse_continuous(results)

    def _parse_continuous(self, results: List[CmdResult]) -> CollectResult:
        state_result, journal_result = results

        is_active = state_result.exit_code == 0 and state_result.stdout.strip() == 'active'
        metrics = {'active': 1.0 if is_active else 0.0}

        journal_ok, log_lines = self._parse_journal(journal_result)
        if not journal_ok:
            return CollectResult(
                metrics=metrics,
                logs=[(f"Log collection failed: {journal_result.stderr}", "ERROR")],
                status='failed',
            )

        return CollectResult(
            metrics=metrics,
            log_lines=log_lines,
            status='online' if is_active else 'warning',
        )

    def _parse_oneshot(self, results: List[CmdResult]) -> CollectResult:
        state_result, journal_result = results

        if state_result.exit_code != 0:
            return CollectResult.failed(f"Failed to query service state: {state_result.stderr}")

        tokens = dict(tok.split('=', 1) for tok in state_result.stdout.strip().split() if '=' in tok)
        result    = tokens.get('result', 'empty')
        exit_code = tokens.get('exit',   'empty')
        active    = tokens.get('active', 'unknown')
        sub       = tokens.get('sub',    'unknown')
        try:
            epoch = int(tokens.get('epoch', '0'))
        except ValueError:
            epoch = 0

        logs = [(
            f"systemd state: result={result!r} exit_code={exit_code!r} epoch={epoch} active={active!r} sub={sub!r}",
            "INFO",
        )]

        is_running = active == 'activating' or (active == 'active' and sub in _RUNNING_SUBSTATES)
        is_success = result == 'success' or exit_code == '0'
        age = (int(time.time()) - epoch) if epoch > 0 else -1

        metrics = {
            'last_run_epoch': float(epoch),
            'last_run_success': 1.0 if is_success else 0.0,
            'is_running': 1.0 if is_running else 0.0,
        }

        if is_running:
            logs.append(("Service is currently running", "INFO"))
            status = 'online'
        elif epoch == 0:
            logs.append(("Service has never run", "WARNING"))
            status = 'failed'
        elif not is_success:
            logs.append((f"Last run failed (result: {result}, exit: {exit_code})", "ERROR"))
            status = 'failed'
        elif age > self.max_age:
            logs.append((
                f"Last run was {format_age(age)}, exceeds max_age of {format_duration(self.max_age)}",
                "WARNING",
            ))
            status = 'failed'
        else:
            logs.append((f"Last run {format_age(age)}, result: {result}", "INFO"))
            status = 'online'

        journal_ok, log_lines = self._parse_journal(journal_result)
        return CollectResult(metrics=metrics, logs=logs, log_lines=log_lines, status=status)

    def _oneshot_state_cmd(self) -> str:
        return (
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

    @staticmethod
    def _parse_journal(journal_result: CmdResult):
        if journal_result.exit_code != 0:
            return False, []
        log_lines = []
        for line in journal_result.stdout.splitlines():
            if not line.strip():
                continue
            log_time, message = SystemdCollectorPlugin._split_iso_line(line)
            level = 'ERROR' if any(k in line.upper() for k in ('ERROR', 'FAIL', 'CRITICAL')) else 'INFO'
            log_lines.append((message, level, log_time))
        return True, log_lines

    @staticmethod
    def _split_iso_line(line: str):
        parts = line.split(' ', 1)
        if len(parts) == 2 and 'T' in parts[0] and parts[0][:4].isdigit():
            return parts[0], line
        return None, line

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {'name': 'Restart Service', 'action_id': 'restart_service', 'variant': 'primary', 'icon': 'play_arrow'},
            {'name': 'Stop Service',    'action_id': 'stop_service',    'variant': 'danger',  'icon': 'stop'},
            {'name': 'Enable on Boot',  'action_id': 'enable_service', 'variant': 'secondary', 'icon': 'toggle_on'},
            {'name': 'Disable on Boot', 'action_id': 'disable_service','variant': 'secondary', 'icon': 'toggle_off'},
        ]

    def plan_action(self, action_id: str, **kwargs) -> Optional[Union[ActionPlan, CollectResult]]:
        command_map = {
            'restart_service': 'restart',
            'stop_service': 'stop',
            'enable_service': 'enable',
            'disable_service': 'disable',
        }
        command = command_map.get(action_id)
        if command is not None:
            return ActionPlan(f"sudo systemctl {command} {self.service_name}")

        if action_id == 'daemon_reload':
            return ActionPlan('sudo systemctl daemon-reload')

        if action_id == 'view_unit_file':
            return ActionPlan(f"sudo systemctl cat {shlex.quote(self.service_name)}")

        if action_id == 'write_unit_file':
            import base64
            content_b64 = base64.b64encode(kwargs.get('content', '').encode('utf-8')).decode('ascii')
            allowed_checks = ' || '.join(
                f'"$P" = {shlex.quote(p)} || case "$P" in {shlex.quote(p + os.sep)}*) true;; *) false;; esac'
                for p in self.allowed_write_paths
            )
            cmd = (
                f"P=$(systemctl show -p FragmentPath --value {shlex.quote(self.service_name)}); "
                "if [ -z \"$P\" ] || [ \"$P\" = 'n/a' ]; then echo 'Unit file path unavailable' >&2; exit 1; fi; "
                f"if ! ( {allowed_checks} ); then echo \"Write path not allowed: $P\" >&2; exit 1; fi; "
                f"echo {shlex.quote(content_b64)} | base64 -d | sudo tee \"$P\" > /dev/null"
            )
            return ActionPlan(cmd)

        return None

    def interpret_action(self, action_id: str, result: CmdResult, **kwargs):
        if action_id in ('view_unit_file',):
            if result.exit_code != 0:
                return CollectResult.failed(result.stderr.strip() or 'Unable to read unit file')
            return CollectResult(success=True, metadata={'content': result.stdout})

        if action_id == 'write_unit_file':
            if result.exit_code != 0:
                return CollectResult.failed(result.stderr.strip() or 'Unable to write unit file')
            return CollectResult(logs=[('Unit file written successfully', 'INFO')], success=True)

        if result.exit_code != 0:
            command_map = {
                'restart_service': 'restart',
                'stop_service': 'stop',
                'enable_service': 'enable',
                'disable_service': 'disable',
            }
            command = command_map.get(action_id, action_id)
            return CollectResult.failed(f"systemctl {command} failed: {result.stderr}")
        return True


_UNIT_FILE_DIALOGS = {
    'view_unit_file': {'kind': 'read', 'title': 'Unit File: {plugin.service_name}',
                       'action_id': 'view_unit_file', 'params': {}, 'render': 'textarea_readonly'},
    'edit_unit_file': {'kind': 'edit', 'title': 'Edit Unit File: {plugin.service_name}',
                       'load_action_id': 'view_unit_file', 'load_params': {},
                       'save_action_id': 'write_unit_file', 'save_params': {}, 'save_content_kwarg': 'content',
                       'success_message': 'Unit file written successfully'},
}


class SystemdUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)

        from vigil.web.ui.spec import register_enabled_predicate
        self._edit_predicate_name = f'systemd_edit_{self.id}'
        register_enabled_predicate(self._edit_predicate_name)(lambda p: p.allow_unit_file_edit)

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
    def UI_SPEC(self):
        return {'dialogs': _UNIT_FILE_DIALOGS}

    def _unit_file_buttons_spec(self) -> List[Dict[str, Any]]:
        return [
            {'id': 'view_unit_file', 'label': 'View Unit File', 'icon': 'article',
             'color': 'primary', 'kind': 'dialog', 'dialog': 'view_unit_file'},
            {'id': 'daemon_reload', 'label': 'Reload Daemon', 'icon': 'refresh',
             'color': 'secondary', 'kind': 'dispatch'},
            {'id': 'edit_unit_file', 'label': 'Edit Unit File', 'icon': 'edit',
             'color': 'secondary', 'kind': 'dialog', 'dialog': 'edit_unit_file',
             'visible_if': self._edit_predicate_name},
        ]

    def _render_unit_file_controls(self):
        from nicegui import ui
        from vigil.web.ui.components import render_buttons

        with ui.card().classes('p-4 h-full'):
            ui.label('Unit File').classes('font-bold mb-2')
            render_buttons(self, self._unit_file_buttons_spec())

    def render_ui(self, context: str = 'page'):
        if self.max_age is not None:
            self._render_oneshot_ui(context)
        else:
            self._render_continuous_ui(context)

    def _render_continuous_ui(self, context: str = 'page'):
        from vigil.core.data.database import StatusHistory
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _CONTINUOUS_LAYOUT if context == 'page' else make_inline_layout(_CONTINUOUS_LAYOUT))
        page = self.ui.page()

        with layout.cell('host_card'):
            self.ui.host_card()
        with layout.cell('service_card'):
            info_card('SERVICE', self.service_name)
        with layout.cell('status_card'):
            self.ui.status_card(
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
            self.ui.logs_table(page, title='LOGS', limit=100, full_height=True)

        def update_time():
            last = StatusHistory.select().where(
                StatusHistory.collector_id == self.id
            ).order_by(StatusHistory.timestamp.desc()).first()
            if last:
                time_label.text = last.timestamp.strftime('%H:%M:%S')

        page.on_refresh(update_time)
        page.start()

    def _render_oneshot_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.web.ui.theme import STATUS_COLORS
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _ONESHOT_LAYOUT if context == 'page' else make_inline_layout(_ONESHOT_LAYOUT))
        page = self.ui.page(metric_names=['is_running', 'last_run_epoch', 'last_run_success'])

        with layout.cell('host_card'):
            self.ui.host_card()
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
            self.ui.logs_table(page, title='LOGS', limit=100, full_height=True)

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
