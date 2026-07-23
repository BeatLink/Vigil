import os
import shlex
import asyncio
from typing import Any, Dict, List, Optional
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.common.time_utils import parse_duration
from vigil.core.ui.components import info_card, on_data_event
from vigil.core.ui.theme import STATUS_COLORS

_DEFAULT_LAYOUT = [
    ['host_card', 'count_card', 'reload_card'],
    ['table'],
    ['events'],
]

_DEFAULT_UNIT_FILE_WRITE_PATHS = (
    '/etc/systemd/system',
    '/run/systemd/system',
    '/lib/systemd/system',
    '/usr/lib/systemd/system',
)


class ServiceListPlugin(BasePlugin):
    """
    Lists all systemd service units on a host and exposes management actions.

    This plugin fetches `systemctl list-units --type=service --all` plus
    `systemctl list-unit-files --type=service --all` to show active, loaded,
    enabled/disabled, and description state for every service.
    """

    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self.max_logs = int(config.get('lines', 10))
        self.allow_unit_file_edit = bool(config.get('allow_unit_file_edit', False))
        self.allowed_write_paths = tuple(config.get('allowed_write_paths', _DEFAULT_UNIT_FILE_WRITE_PATHS))
        self._services: List[Dict[str, Any]] = []

    async def on_collect(self):
        await self._collect_services()

    async def _collect_services(self):
        list_units_cmd = (
            'systemctl list-units --type=service --all '
            '--no-legend --no-pager --plain'
        )
        status, stdout, stderr = await self.ssh_collector.fetch_output(list_units_cmd)
        if status != 0:
            self.db_logger.write(f'Collection failed: {stderr}', level='ERROR')
            self.set_status('failed')
            return

        services = self._parse_unit_list(stdout)

        list_unit_files_cmd = (
            'systemctl list-unit-files --type=service --all '
            '--no-legend --no-pager'
        )
        status2, stdout2, stderr2 = await self.ssh_collector.fetch_output(list_unit_files_cmd)
        if status2 != 0:
            self.db_logger.write(f'Unit-file collection failed: {stderr2}', level='ERROR')
            self.set_status('failed')
            return

        enabled_map = self._parse_unit_file_list(stdout2)
        for service in services:
            service['enabled'] = enabled_map.get(service['unit'], 'unknown')

        self._services = services
        total = len(services)
        active = sum(1 for s in services if s['active'] == 'active')
        failed = sum(1 for s in services if s['active'] == 'failed')

        self.db_metrics.metric('services_total', float(total))
        self.db_metrics.metric('services_active', float(active))
        self.db_metrics.metric('services_failed', float(failed))
        self.db_logger.write(
            f'Collected {total} services, {active} active, {failed} failed',
            level='INFO'
        )
        self.set_status('online')

    @staticmethod
    def _parse_unit_list(stdout: str) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            unit, load, active, sub, description = parts
            services.append({
                'unit': unit,
                'load': load,
                'active': active,
                'sub': sub,
                'description': description,
            })
        return services

    @staticmethod
    def _parse_unit_file_list(stdout: str) -> Dict[str, str]:
        enabled_map: Dict[str, str] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            unit_file = parts[0]
            state = parts[1]
            enabled_map[unit_file] = state
        return enabled_map

    async def _resolve_unit_path(self, service_name: str) -> tuple[bool, str]:
        status, stdout, stderr = await self.ssh_controller.execute_action(
            f'systemctl show -p FragmentPath --value {shlex.quote(service_name)}'
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

    async def _read_unit_file(self, service_name: str) -> tuple[bool, str]:
        status, stdout, stderr = await self.ssh_controller.execute_action(
            f'sudo systemctl cat {shlex.quote(service_name)}'
        )
        if status != 0:
            return False, stderr.strip() or 'Unable to read unit file'
        return True, stdout

    async def _write_unit_file(self, service_name: str, content: str) -> tuple[bool, str]:
        ok, path_or_error = await self._resolve_unit_path(service_name)
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

    async def _run_systemctl_command(self, command: str, service_name: Optional[str] = None) -> bool:
        target = f' {shlex.quote(service_name)}' if service_name and command != 'daemon-reload' else ''
        status, _, stderr = await self.ssh_controller.execute_action(
            f'sudo systemctl {command}{target}'.strip()
        )
        if status != 0:
            self.db_logger.write(f'systemctl {command} failed for {service_name}: {stderr}', level='ERROR')
        return status == 0

    def get_actions(self) -> List[Dict[str, str]]:
        return [
            {
                'name': 'Reload Daemon',
                'action_id': 'daemon_reload',
                'variant': 'secondary',
                'icon': 'refresh',
            },
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        service_name = kwargs.get('service_name')
        if action_id in ('start_service', 'stop_service', 'restart_service', 'enable_service', 'disable_service'):
            if not service_name:
                self.db_logger.write('Service action missing service_name', level='ERROR')
                return False
            command = action_id.replace('_service', '')
            return await self._run_systemctl_command(command, service_name)

        if action_id == 'daemon_reload':
            return await self._run_systemctl_command('daemon-reload')

        return False

    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.core.ui.layout import PluginLayout, make_inline_layout

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()

        with layout.cell('count_card'):
            self._count_label = info_card('SERVICES', '--')

        with layout.cell('reload_card'):
            ui.button('Reload Daemon', on_click=lambda: asyncio.create_task(self._reload_daemon()), color='secondary').props('flat')

        with layout.cell('table'):
            search_in = ui.input('Filter services').props('outlined dense clearable').classes('w-full mb-4')
            columns = [
                {'name': 'unit',        'label': 'Unit',        'field': 'unit',        'sortable': True,  'align': 'left'},
                {'name': 'load',        'label': 'Load',        'field': 'load',        'sortable': True,  'align': 'left'},
                {'name': 'active',      'label': 'Active',      'field': 'active',      'sortable': True,  'align': 'left'},
                {'name': 'sub',         'label': 'Sub',         'field': 'sub',         'sortable': True,  'align': 'left'},
                {'name': 'enabled',     'label': 'Enabled',     'field': 'enabled',     'sortable': True,  'align': 'left'},
                {'name': 'description', 'label': 'Description', 'field': 'description', 'sortable': True,  'align': 'left'},
                {'name': 'actions',     'label': '',           'field': 'actions',     'sortable': False, 'align': 'center'},
            ]

            table = ui.table(columns=columns, rows=[], row_key='unit').classes('w-full text-sm')
            table.add_slot('body-cell-actions', '''
<q-td :props="props" class="q-pa-none">
  <div class="row items-center q-gutter-xs">
    <q-btn dense flat icon="play_arrow" color="positive" size="sm"
           @click="$parent.$emit('start_service', props.row)"
           title="Start Service" />
    <q-btn dense flat icon="stop" color="warning" size="sm"
           @click="$parent.$emit('stop_service', props.row)"
           title="Stop Service" />
    <q-btn dense flat icon="replay" color="primary" size="sm"
           @click="$parent.$emit('restart_service', props.row)"
           title="Restart Service" />
    <q-btn dense flat icon="toggle_on" color="secondary" size="sm"
           @click="$parent.$emit('enable_service', props.row)"
           title="Enable on Boot" />
    <q-btn dense flat icon="toggle_off" color="secondary" size="sm"
           @click="$parent.$emit('disable_service', props.row)"
           title="Disable on Boot" />
    <q-btn dense flat icon="info" color="info" size="sm"
           @click="$parent.$emit('view_status', props.row)"
           title="View Status" />
    <q-btn dense flat icon="article" color="secondary" size="sm"
           @click="$parent.$emit('view_file', props.row)"
           title="View Unit File" />
    ''' + ('' if not self.allow_unit_file_edit else '''
    <q-btn dense flat icon="edit" color="secondary" size="sm"
           @click="$parent.$emit('edit_file', props.row)"
           title="Edit Unit File" />
    ''') + '''
  </div>
</q-td>
''')

            def update_table():
                filter_term = (search_in.value or '').strip().lower()
                rows = []
                for service in self._services:
                    if filter_term:
                        haystack = ' '.join(
                            str(service.get(key, '')).lower()
                            for key in ('unit', 'load', 'active', 'sub', 'enabled', 'description')
                        )
                        if filter_term not in haystack:
                            continue
                    rows.append(service)
                table.rows = rows
                table.update()

            async def do_row_action(e, action: str):
                service_name = (e.args or {}).get('unit')
                if not service_name:
                    return
                if action == 'view_status':
                    await self._show_status(service_name)
                    return
                if action == 'view_file':
                    await self._show_unit_file(service_name)
                    return
                if action == 'edit_file':
                    await self._edit_unit_file(service_name)
                    return
                action_id = f'{action}_service'
                success = await self.on_action(action_id, service_name=service_name)
                ui.notify(
                    f'{action.replace("_", " ").title()} {"succeeded" if success else "failed"}',
                    type='positive' if success else 'negative'
                )

            table.on('start_service', lambda e: asyncio.create_task(do_row_action(e, 'start')))
            table.on('stop_service', lambda e: asyncio.create_task(do_row_action(e, 'stop')))
            table.on('restart_service', lambda e: asyncio.create_task(do_row_action(e, 'restart')))
            table.on('enable_service', lambda e: asyncio.create_task(do_row_action(e, 'enable')))
            table.on('disable_service', lambda e: asyncio.create_task(do_row_action(e, 'disable')))
            table.on('view_status', lambda e: asyncio.create_task(do_row_action(e, 'view_status')))
            table.on('view_file', lambda e: asyncio.create_task(do_row_action(e, 'view_file')))
            if self.allow_unit_file_edit:
                table.on('edit_file', lambda e: asyncio.create_task(do_row_action(e, 'edit_file')))

            def update():
                self._count_label.text = str(len(self._services))
                update_table()

            search_in.on('update:modelValue', lambda e: update())
            on_data_event('metric', table, update)

        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](title='PLUGIN EVENTS')

    async def _show_status(self, service_name: str):
        from nicegui import ui

        status, stdout, stderr = await self.ssh_controller.execute_action(
            f'sudo systemctl status {shlex.quote(service_name)} --no-pager'
        )
        if status != 0:
            ui.notify(stderr or 'Unable to fetch service status', type='negative')
            return

        with ui.dialog() as dialog:
            ui.dialog_title(f'Status: {service_name}')
            ui.markdown('''
```text
''')
            ui.label(stdout).classes('font-mono text-xs').style('white-space: pre-wrap;')
            ui.markdown('''
```''')
            ui.button('Close', on_click=dialog.close).props('flat')
        dialog.open()

    async def _show_unit_file(self, service_name: str):
        from nicegui import ui

        ok, content = await self._read_unit_file(service_name)
        if not ok:
            ui.notify(content, type='negative')
            return

        with ui.dialog() as dialog:
            ui.dialog_title(f'Unit File: {service_name}')
            ui.textarea(content, readonly=True, auto_grow=True).classes('w-full')
            ui.button('Close', on_click=dialog.close).props('flat')
        dialog.open()

    async def _edit_unit_file(self, service_name: str):
        from nicegui import ui

        if not self.allow_unit_file_edit:
            ui.notify('Unit file editing is disabled', type='negative')
            return

        ok, content = await self._read_unit_file(service_name)
        if not ok:
            ui.notify(content, type='negative')
            return

        with ui.dialog() as dialog:
            ui.dialog_title(f'Edit Unit File: {service_name}')
            editor = ui.textarea(content, auto_grow=True).classes('w-full h-96')
            with ui.row().classes('justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')

                async def save():
                    save_ok, message = await self._write_unit_file(service_name, editor.value)
                    ui.notify(message, type='positive' if save_ok else 'negative')
                    if save_ok:
                        dialog.close()

                ui.button('Save', on_click=save).props('flat primary')
        dialog.open()

    async def _reload_daemon(self):
        from nicegui import ui

        success = await self.on_action('daemon_reload')
        ui.notify('Daemon reloaded' if success else 'Daemon reload failed', type='positive' if success else 'negative')
