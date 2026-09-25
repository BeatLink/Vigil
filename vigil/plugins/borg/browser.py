"""The borg monitor's page: its declarative spec, plus a file browser that lists an archive one folder at a time and restores the ticked paths."""

from typing import Any, Dict, List


def render_page(plugin, context: str = 'page'):
    """Render the spec's widgets, then the browser into its own layout cell."""
    from vigil.core.ui.layout import PluginLayout, make_inline_layout
    from vigil.core.ui.spec import generic_render

    rows = plugin.UI_SPEC['layout']
    layout = PluginLayout(plugin.config, rows if context == 'page' else make_inline_layout(rows))
    page = generic_render(plugin, context, layout=layout, start=False)
    with layout.cell('browser'):
        render_browser(plugin)
    page.start()


def _rows(entries: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    from vigil.plugins.borg.parsing import _format_size
    return [
        {'name': e['name'], 'path': e['path'], 'dir': e['dir'],
         'size': '' if e['dir'] or e['size'] is None else _format_size(e['size']),
         'mtime': e['mtime']}
        for e in entries[:limit]
    ]


def render_browser(plugin):
    """An archive picker, a breadcrumb trail, the current folder's entries with tick boxes, and a restore button."""
    from nicegui import ui
    from vigil.core.ui.components import LABEL_CLASS, action_button, card, confirmed, on_data_event

    state = {'archive': None, 'path': ''}

    async def open_folder(archive, path: str):
        if not archive:
            return
        state.update(archive=archive, path=path)
        draw_crumbs()
        table.selected = []
        count_selected()
        entries = plugin.listing(archive, path)
        if entries is None:
            status.text = 'Listing… borg reads the whole archive to list a folder, so a large one takes a while.'
            table.rows = []
            table.update()
            ok, content = await plugin.run_action('browse_archive', archive=archive, path=path)
            # The user may have opened another folder while this one was being listed.
            if (state['archive'], state['path']) != (archive, path):
                return
            entries = plugin.listing(archive, path)
            if not ok or entries is None:
                status.text = content or 'Listing failed'
                return
        limit = plugin.browse_limit
        if not entries:
            status.text = 'Nothing at this path.'
        elif len(entries) > limit:
            status.text = f'Showing the first {limit} entries — raise browse_limit to see more.'
        else:
            status.text = f'{len(entries)} entries'
        table.rows = _rows(entries, limit)
        table.update()

    def draw_crumbs():
        crumbs.clear()
        with crumbs:
            parts = [p for p in state['path'].split('/') if p]
            ui.button('/', on_click=lambda: open_folder(state['archive'], '')).props('flat dense no-caps size=sm')
            for depth, part in enumerate(parts):
                target = '/'.join(parts[:depth + 1])
                ui.label('›').classes('halon-caption')
                ui.button(part, on_click=lambda t=target: open_folder(state['archive'], t)).props('flat dense no-caps size=sm')

    def count_selected():
        n = len(table.selected)
        restore_btn.text = f'Restore {n} selected' if n else 'Restore selected'
        restore_btn.set_enabled(bool(n))

    async def restore():
        paths = [row['path'] for row in table.selected]
        if not paths or not state['archive']:
            return
        shown = '\n'.join(f'  /{p}' for p in paths[:10])
        if len(paths) > 10:
            shown += f'\n  … and {len(paths) - 10} more'
        if not await confirmed(f"Restore from {state['archive']} into a new folder under "
                               f"{plugin.restore_dir}? Live files are not touched.\n\n{shown}"):
            return
        ok, content = await plugin.run_action('restore_archive', archive=state['archive'], paths=paths)
        ui.notify(content or ('Restore started' if ok else 'Restore failed'), type='positive' if ok else 'negative')
        if ok:
            table.selected = []
            count_selected()

    async def on_open(e):
        row = e.args or {}
        if row.get('dir'):
            await open_folder(state['archive'], row.get('path', ''))

    with card('w-full'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label('BROWSE ARCHIVE').classes(LABEL_CLASS)
            picker = ui.select([], label='Archive',
                               on_change=lambda e: open_folder(e.value, '')).props('outlined dense').classes('min-w-[18rem]')
        crumbs = ui.row().classes('items-center gap-1')
        status = ui.label('Pick an archive to browse its files.').classes('halon-caption mb-2')
        table = ui.table(
            columns=[
                {'name': 'name', 'label': 'Name', 'field': 'name', 'align': 'left', 'sortable': True},
                {'name': 'size', 'label': 'Size', 'field': 'size', 'align': 'right'},
                {'name': 'mtime', 'label': 'Modified', 'field': 'mtime', 'align': 'left', 'sortable': True},
            ],
            rows=[], row_key='path', selection='multiple', on_select=count_selected,
            pagination={'rowsPerPage': 50},
        ).classes('w-full')
        table.add_slot('body-cell-name', '''
<q-td :props="props">
  <span :class="props.row.dir ? 'cursor-pointer' : ''" @click="props.row.dir && $parent.$emit('open', props.row)">
    <q-icon :name="props.row.dir ? 'folder' : 'description'" size="xs" class="q-mr-sm" />{{ props.row.name }}
  </span>
</q-td>
''')
        table.on('open', on_open)
        with ui.row().classes('w-full justify-end mt-2'):
            restore_btn = action_button('Restore selected', icon='restore', weight='filled', on_click=restore)
        restore_btn.set_enabled(False)

    def update_archives():
        names = [a.get('name') for a in plugin.cached_archives()[0] if a.get('name')]
        if names != picker.options:
            picker.set_options(names)
        if state['archive'] and state['archive'] not in names:
            state.update(archive=None, path='')
            crumbs.clear()
            table.rows = []
            table.update()
            status.text = 'That archive is gone from the list. Pick another.'

    on_data_event(update_archives)
