import asyncio
from typing import Optional
from nicegui import ui
from vigil.core.data.events import bus
from .theme import TEXT, TEXT_MUTED, PRIMARY, ACCENT, STATUS_COLORS, BACKGROUND_MUTED, BACKGROUND


def offload(read_fn):
    async def _run(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: read_fn(*args, **kwargs))
    return _run


def refresh_rows(table, new_rows) -> None:
    if new_rows != table.rows:
        table.rows = new_rows
        table.update()


class _SafeTimer(ui.timer):
    def _detached(self) -> bool:
        if getattr(self, "is_deleted", False):
            return True
        try:
            return self.id not in self.client.elements
        except Exception:
            return True

    def _should_stop(self) -> bool:
        return self._detached() or super()._should_stop()


def safe_timer(interval: float, callback, defer_first: bool = False):
    from nicegui import helpers
    timer = None

    async def _wrapped():
        try:
            result = callback()
            if helpers.should_await(result):
                await result
        except RuntimeError as e:
            if 'parent slot' in str(e) or 'has been deleted' in str(e):
                if timer is not None:
                    timer.cancel()
                return
            raise

    timer = _SafeTimer(interval, _wrapped, immediate=not defer_first)
    return timer


POLL_FALLBACK_SECONDS = 1.0


def on_data_event(event, element, callback, run_now: bool = True):
    if bus.polling_mode:
        safe_timer(POLL_FALLBACK_SECONDS, callback, defer_first=not run_now)
        return

    events = [event] if isinstance(event, str) else list(event)

    def _detached() -> bool:
        if getattr(element, 'is_deleted', False):
            return True
        try:
            return element.id not in element.client.elements
        except Exception:
            return True

    from nicegui import helpers
    offs: list = []

    def _unsubscribe():
        for off in offs:
            off()

    async def _wrapped():
        if _detached():
            _unsubscribe()
            return
        try:
            result = callback()
            if helpers.should_await(result):
                await result
        except RuntimeError as e:
            if 'parent slot' not in str(e) and 'has been deleted' not in str(e):
                raise
            _unsubscribe()

    offs.extend(bus.on(ev, _wrapped) for ev in events)
    element.client.on_disconnect(_unsubscribe)
    if run_now:
        _wrapped()


LABEL_CLASS = 'text-xs font-bold'
VALUE_CLASS = 'text-2xl font-bold'
SECTION_CLASS = 'text-xl font-bold'
HOVER_STYLE = 'hover:bg-blue-50 cursor-pointer'

def card(classes: str = '', padding: bool = True):
    p = 'p-4' if padding else 'p-0'
    return ui.card().classes(f'{p} shadow-sm {classes}')

def info_card(title: str, value: str = '--', value_classes: str = VALUE_CLASS, card_classes: str = 'flex-1 min-w-[9rem]'):
    with card(f'h-28 overflow-hidden items-center justify-center {card_classes}'):
        ui.label(title.upper()).classes(LABEL_CLASS).style(f'color: {TEXT_MUTED}')
        return ui.label(value).classes(f'{value_classes} w-full text-center break-words').style(f'color: {PRIMARY}')

def action_chip(text: str, on_click=None, icon: str = 'play_arrow', color: str = PRIMARY):
    return ui.chip(text, icon=icon, on_click=on_click, color=color, text_color=BACKGROUND).props('clickable')

def section_title(text: str, classes: str = ''):
    return ui.label(text).classes(f'{SECTION_CLASS} mb-4 {classes}').style(f'color: {TEXT}')

def metric_table(page, collector: str, title: str = 'Monitor Metrics', limit: int = 15):
    with card():
        ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')
        table = ui.table(columns=[
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left'},
            {'name': 'name', 'label': 'Metric', 'field': 'metric_name', 'align': 'left'},
            {'name': 'val', 'label': 'Value', 'field': 'value', 'align': 'left'},
        ], rows=[]).classes('w-full border-none')

        def _read():
            return page.plugin.db.collector_metrics_cached(collector, limit=limit)

        async def update():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update)
        return table

def log_table(page, target: str, filter_prefix: str = '', title: str = 'Recent Logs',
             limit: int = 15, full_height: bool = False):
    card_classes = 'w-full overflow-hidden flex-grow' if full_height else ''

    with card(card_classes, padding=not full_height):
        if full_height:
            ui.label(title).classes('font-bold p-4 w-full border-b').style(f'background-color: {BACKGROUND_MUTED}; color: {PRIMARY}')
        else:
            ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')

        columns = [
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
            {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
            {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left',
             'classes': 'text-wrap font-mono text-xs' if full_height else ''},
        ]

        table_classes = 'w-full border-none'
        if full_height:
            table_classes += ' h-[600px]'

        table = ui.table(columns=columns, rows=[]).classes(table_classes)
        if full_height:
            table.props('virtual-scroll')

        def _read():
            return page.plugin.db.log_lines_cached(target, filter_prefix, limit=limit)

        async def update_logs():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update_logs)
        return table

def event_table(page, plugin_name: str, plugin_id: str = '', target: str = '',
                title: str = 'Recent Events', limit: int = 100,
                full_height: bool = False):
    prefix = f"[{plugin_name}] "
    card_classes = 'w-full overflow-hidden flex-grow' if full_height else ''

    with card(card_classes, padding=not full_height):
        if full_height:
            ui.label(title).classes('font-bold p-4 w-full border-b').style(
                f'background-color: {BACKGROUND_MUTED}; color: {PRIMARY}')
        else:
            ui.label(title).classes('font-bold mb-2').style(f'color: {PRIMARY}')

        columns = [
            {'name': 'ts', 'label': 'Time', 'field': 'timestamp', 'align': 'left', 'sortable': True},
            {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
            {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left',
             'classes': 'text-wrap font-mono text-xs' if full_height else ''},
        ]
        table_classes = 'w-full border-none' + (' h-[600px]' if full_height else '')
        table = ui.table(columns=columns, rows=[]).classes(table_classes)
        if full_height:
            table.props('virtual-scroll')

        def _read():
            return page.plugin.db.plugin_events_cached(plugin_id, prefix, target, limit=limit)

        async def update():
            refresh_rows(table, await offload(_read)())

        page.on_refresh(update)
        return table


def history_chart(page, title: str, collector: str, metric_name: str, limit: int = 30):
    with card('w-full h-80 mb-4 p-2', padding=False):
        ui.label(title.upper()).classes(f'{LABEL_CLASS} mb-1')
        chart = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'grid': {'left': 4, 'right': 8, 'top': 8, 'bottom': 4, 'containLabel': True},
            'xAxis': {'type': 'category', 'data': []},
            'yAxis': {'type': 'value', 'splitLine': {'show': False}},
            'series': [{
                'data': [],
                'type': 'line',
                'smooth': True,
                'color': PRIMARY,
                'areaStyle': {'opacity': 0.1}
            }]
        }).classes('w-full h-72')

        def _read():
            history = page.plugin.db.metric_history_cached(collector, metric_name, limit=limit)
            return (
                [m.timestamp.strftime('%H:%M:%S') for m in history],
                [m.value for m in history],
            )

        def _apply(data):
            x, y = data
            if x == chart.options['xAxis']['data'] and y == chart.options['series'][0]['data']:
                return
            chart.options['xAxis']['data'] = x
            chart.options['series'][0]['data'] = y
            chart.update()

        async def update():
            _apply(await offload(_read)())

        page.on_refresh(update)
        return chart

def chip_label(label: str, value: str, color: Optional[str] = None):
    style = f'background: {color}22; color: {color}' if color else f'color: {TEXT}'
    return ui.label(f'{label}: {value}' if label else value).classes(
        'px-2 py-1 rounded text-sm font-mono').style(style)


def _resolve_repeat_items(plugin, repeat_spec: dict) -> list:
    source = repeat_spec.get('source', 'snapshot')

    if source == 'setting':
        import json
        key = repeat_spec.get('setting_key', '').format(plugin_id=plugin.id)
        raw = plugin.storage.get_setting(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            dict_fields = repeat_spec.get('dict_fields')
            if dict_fields:
                # Project selected present keys of a single settings dict into
                # one {'label': ..., 'value': ...} item per key (e.g. cloud.py's
                # instance_id/region/az/zone, only the ones the provider set).
                return [
                    {'label': key.replace('_', ' ').upper(), 'value': str(data[key])}
                    for key in dict_fields if data.get(key)
                ]
            return [data]
        # A list of plain strings (e.g. DNS answers) becomes {'label': ..., 'value': ...}
        # items so the same item_label/item_value machinery applies uniformly.
        return [
            item if isinstance(item, dict) else {'label': item, 'value': item}
            for item in data
        ]

    if source == 'metrics_prefix':
        return _resolve_metrics_prefix_items(plugin, repeat_spec)

    return plugin.storage.latest_snapshot(default=[])


def _scan_metric_family(plugin, prefix: str, suffix: str, exclude: set, limit: int) -> dict:
    """Returns {stripped_key: latest_value} for metric names matching prefix/suffix."""
    from vigil.core.data.database import Metric

    query = Metric.select().where(Metric.collector == plugin.id)
    if prefix:
        query = query.where(Metric.metric_name.startswith(prefix))
    if suffix:
        query = query.where(Metric.metric_name.endswith(suffix))
    query = query.order_by(Metric.timestamp.desc()).limit(limit)

    latest: dict = {}
    for row in query:
        if row.metric_name in exclude or row.metric_name in latest:
            continue
        key = row.metric_name
        if prefix:
            key = key.removeprefix(prefix)
        if suffix:
            key = key.removesuffix(suffix)
        latest[key] = row.value
    return latest


def _resolve_metrics_prefix_items(plugin, repeat_spec: dict) -> list:
    """Items discovered from metric names matching a prefix/suffix pattern
    rather than a snapshot blob — for plugins that record one (or more)
    metric(s) per dynamic item (folder_<name>_gb, fs_<name>_used_pct,
    gpu<N>_util, ...). `fields` (optional) lets multiple metric families
    sharing the same stripped key merge into one item dict, e.g.
    fs_<key>_used_pct + fs_<key>_inodes_pct -> {'key':.., 'used_pct':..,
    'inodes_pct':..}. Without `fields`, falls back to the single
    metrics_prefix/metrics_suffix pair -> {'key':.., 'value':..}."""
    exclude = set(repeat_spec.get('metrics_exclude', []))
    limit = repeat_spec.get('metrics_scan_limit', 200)

    fields = repeat_spec.get('fields')
    if fields:
        merged: dict = {}
        for field in fields:
            family = _scan_metric_family(
                plugin, field.get('prefix', ''), field.get('suffix', ''), exclude, limit)
            for key, value in family.items():
                merged.setdefault(key, {'key': key})[field['name']] = value
        items = list(merged.values())
    else:
        family = _scan_metric_family(
            plugin, repeat_spec.get('metrics_prefix', ''),
            repeat_spec.get('metrics_suffix', ''), exclude, limit)
        items = [{'key': key, 'value': value} for key, value in family.items()]

    items.sort(key=lambda i: i['key'])
    return items


_LABEL_TRANSFORMS = {
    'slashes': lambda s: '/' + s.replace('_', '/') if s != 'root' else '/',
    'spaces_upper': lambda s: s.replace('_', ' ').upper(),
    'none': lambda s: s,
}


def render_repeat_card(plugin, page, repeat_spec: dict):
    from vigil.web.ui.spec import FORMATTERS, ITEM_COLOR_RULES, ITEM_FORMATTERS
    from vigil.web.ui.theme import STATUS_COLORS

    source = repeat_spec.get('source', 'snapshot')
    container_kind = repeat_spec.get('container', 'chips')
    default_label = 'key' if source == 'metrics_prefix' else 'label'
    default_value = 'value'
    item_label = repeat_spec.get('item_label', default_label)
    item_value = repeat_spec.get('item_value', default_value)
    item_format = repeat_spec.get('item_format')
    formatter = FORMATTERS.get(item_format) if item_format else None
    item_format_fn_name = repeat_spec.get('item_format_fn')
    item_format_fn = ITEM_FORMATTERS.get(item_format_fn_name) if item_format_fn_name else None
    label_transform = _LABEL_TRANSFORMS.get(repeat_spec.get('label_transform', 'none'))
    label_prefix = repeat_spec.get('item_label_prefix', '')
    label_suffix = repeat_spec.get('item_label_suffix', '')
    color_rule_name = repeat_spec.get('item_color_by')
    color_rule = ITEM_COLOR_RULES.get(color_rule_name) if color_rule_name else None
    empty_text = repeat_spec.get('empty_text', 'No data')

    wrap_style = (
        'display: flex; flex-wrap: wrap; gap: 0.5rem; width: 100%'
        if container_kind == 'chips' else
        'display: flex; flex-wrap: wrap; gap: 0.75rem; width: 100%'
    )
    container = ui.element('div').style(wrap_style)

    def render():
        items = _resolve_repeat_items(plugin, repeat_spec)
        container.clear()
        if not items:
            with container:
                ui.label(empty_text).classes('text-sm').style(f'color: {TEXT_MUTED}')
            return
        with container:
            for item in items:
                raw_label = str(item.get(item_label, ''))
                label = label_transform(raw_label) if raw_label else raw_label
                if label:
                    label = f'{label_prefix}{label}{label_suffix}'
                if item_format_fn:
                    value = item_format_fn(item)
                else:
                    raw_value = item.get(item_value)
                    value = formatter(raw_value) if formatter else str(raw_value)
                state = color_rule(item) if color_rule else None
                color = STATUS_COLORS.get(state) if state else None
                if container_kind == 'chips':
                    chip_label(label, value, color)
                else:
                    info_card(label or '--', value)

    page.on_refresh(render)


def render_buttons(plugin, button_specs: list):
    from vigil.web.ui.spec import ENABLED_PREDICATES

    with ui.row().classes('gap-2 items-center'):
        for spec in button_specs:
            predicate_name = spec.get('visible_if')
            if predicate_name:
                predicate = ENABLED_PREDICATES.get(predicate_name)
                if predicate is not None and not predicate(plugin):
                    continue

            async def _click(_e=None, s=spec):
                if s.get('kind') == 'dialog':
                    await open_dialog_impl(plugin, s['dialog'])
                    return
                success = await plugin.on_action(s['id'])
                if s.get('notify', True):
                    label = s.get('label', s['id'])
                    ui.notify(
                        f'{label} {"succeeded" if success else "failed"}',
                        type='positive' if success else 'negative',
                    )

            ui.button(
                spec.get('label', spec['id']), icon=spec.get('icon'),
                color=spec.get('color', 'secondary'),
                on_click=lambda e, c=_click: asyncio.create_task(c(e)),
            ).props('flat' if spec.get('flat', True) else '')


def _substitute(template: str, row: Optional[dict], plugin) -> str:
    class _PluginProxy(dict):
        def __missing__(self, key):
            return getattr(plugin, key, '')
    return template.format(row=row or {}, plugin=_PluginProxy())


async def open_dialog_impl(plugin, dialog_name: str, row: Optional[dict] = None):
    from vigil.web.ui.spec import _dialog_spec_for
    spec = _dialog_spec_for(plugin, dialog_name)
    if spec is None:
        ui.notify(f'Unknown dialog {dialog_name!r}', type='negative')
        return

    title = _substitute(spec.get('title', dialog_name), row, plugin)

    def _resolve_params(params_spec: dict) -> dict:
        return {kwarg: (row or {}).get(field) for kwarg, field in (params_spec or {}).items()}

    if spec['kind'] == 'read':
        ok, content = await plugin.action_with_output(spec['action_id'], **_resolve_params(spec.get('params')))
        if not ok:
            ui.notify(content or 'Action failed', type='negative')
            return
        with ui.dialog() as dialog:
            ui.dialog_title(title)
            if spec.get('render') == 'textarea_readonly':
                ui.textarea(content, readonly=True, auto_grow=True).classes('w-full')
            else:
                ui.label(content).classes('font-mono text-xs').style('white-space: pre-wrap;')
            ui.button('Close', on_click=dialog.close).props('flat')
        dialog.open()
        return

    if spec['kind'] == 'edit':
        ok, content = await plugin.action_with_output(
            spec['load_action_id'], **_resolve_params(spec.get('load_params')))
        if not ok:
            ui.notify(content or 'Unable to load content', type='negative')
            return
        with ui.dialog() as dialog:
            ui.dialog_title(title)
            editor = ui.textarea(content, auto_grow=True).classes('w-full h-96')
            with ui.row().classes('justify-end gap-2 mt-4'):
                ui.button('Cancel', on_click=dialog.close).props('flat')

                async def save():
                    save_kwargs = _resolve_params(spec.get('save_params'))
                    save_kwargs[spec.get('save_content_kwarg', 'content')] = editor.value
                    save_ok = await plugin.on_action(spec['save_action_id'], **save_kwargs)
                    ui.notify(
                        spec.get('success_message', 'Saved') if save_ok else 'Save failed',
                        type='positive' if save_ok else 'negative',
                    )
                    if save_ok:
                        dialog.close()

                ui.button('Save', on_click=save).props('flat primary')
        dialog.open()
        return


def render_table_with_actions(plugin, page, table_spec: dict, filter_spec: Optional[dict] = None):
    from vigil.web.ui.spec import ENABLED_PREDICATES, FORMATTERS, ITEM_COLOR_RULES
    from vigil.web.ui.theme import STATUS_COLORS

    row_key = table_spec.get('row_key', 'id')
    columns = list(table_spec.get('columns', []))
    row_actions = [
        a for a in table_spec.get('row_actions', [])
        if not a.get('visible_if') or ENABLED_PREDICATES.get(a['visible_if'], lambda p: True)(plugin)
    ]

    search_in = None
    if filter_spec:
        search_in = ui.input(filter_spec.get('placeholder', 'Filter')).props(
            'outlined dense clearable').classes('w-full mb-4')

    render_columns = list(columns)
    if row_actions:
        render_columns = render_columns + [
            {'name': 'actions', 'label': '', 'field': 'actions', 'sortable': False, 'align': 'center'},
        ]

    table = ui.table(columns=render_columns, rows=[], row_key=row_key).classes('w-full text-sm')

    for col in columns:
        color_rule_name = col.get('cell_color_by')
        if not color_rule_name:
            continue
        rule = ITEM_COLOR_RULES.get(color_rule_name)
        if rule is None:
            continue
        table.add_slot(f'body-cell-{col["name"]}', f'''
            <q-td :props="props">
                <span :style="{{ color: props.row._color_{col['name']} }}">{{{{ props.row.{col['field']} }}}}</span>
            </q-td>
        ''')

    if row_actions:
        buttons_html = ''.join(
            f'''<q-btn dense flat icon="{a['icon']}" color="{a.get('color', 'primary')}" size="sm"
                       @click="$parent.$emit('{a['id']}', props.row)"
                       title="{a.get('tooltip', '')}" />'''
            for a in row_actions
        )
        table.add_slot('body-cell-actions', f'''
<q-td :props="props" class="q-pa-none">
  <div class="row items-center q-gutter-xs">
    {buttons_html}
  </div>
</q-td>
''')

    async def _handle_action(e, action: dict):
        row = e.args or {}
        if action.get('kind') == 'dialog':
            await open_dialog_impl(plugin, action['dialog'], row=row)
            return
        params = {kwarg: row.get(field) for kwarg, field in action.get('params', {}).items()}
        params.update(action.get('static_params', {}))
        action_id = action.get('action_id', action['id'])
        success = await plugin.on_action(action_id, **params)
        if action.get('notify', True):
            label = action.get('tooltip', action_id).replace('_', ' ').title()
            ui.notify(
                f'{label} {"succeeded" if success else "failed"}',
                type='positive' if success else 'negative',
            )

    for action in row_actions:
        table.on(action['id'], lambda e, a=action: asyncio.create_task(_handle_action(e, a)))

    def _rows():
        rows_attr = table_spec.get('rows_attr')
        if rows_attr:
            rows = list(getattr(plugin, rows_attr))
        else:
            rows = _resolve_repeat_items(plugin, {'source': table_spec.get('source', 'snapshot')})
        for col in columns:
            color_rule_name = col.get('cell_color_by')
            if not color_rule_name:
                continue
            rule = ITEM_COLOR_RULES.get(color_rule_name)
            if rule is None:
                continue
            for row in rows:
                state = rule(row)
                row[f"_color_{col['name']}"] = STATUS_COLORS.get(state, STATUS_COLORS['online']) if state else STATUS_COLORS['online']

        if search_in is not None:
            filter_term = (search_in.value or '').strip().lower()
            fields = filter_spec.get('fields', [])
            if filter_term:
                rows = [
                    row for row in rows
                    if filter_term in ' '.join(str(row.get(f, '')).lower() for f in fields)
                ]
        return rows

    def update_table():
        refresh_rows(table, _rows())

    if search_in is not None:
        search_in.on('update:modelValue', lambda e: update_table())

    page.on_refresh(update_table)
    update_table()
    return table


def render_job_panel(plugin, spec: dict):
    from vigil.web.ui.spec import ENABLED_PREDICATES
    from vigil.web.ui.theme import PRIMARY, STATUS_COLORS
    from vigil.core.common.time_utils import format_duration

    history_limit = spec.get('history_limit', 10)
    enabled_name = spec.get('enabled_if')
    enabled_predicate = ENABLED_PREDICATES.get(enabled_name, lambda p: True) if enabled_name else (lambda p: True)

    with card('w-full'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label(spec.get('title', 'JOBS')).classes('font-bold').style(f'color: {PRIMARY}')
            with ui.row().classes('gap-2'):
                run_btn = ui.button(
                    spec.get('run_label', 'Run'), icon=spec.get('run_icon', 'play_arrow'),
                    on_click=lambda: asyncio.create_task(_start(plugin, spec)),
                ).props('dense')
                cancel_btn = ui.button(
                    spec.get('cancel_label', 'Cancel'), icon=spec.get('cancel_icon', 'stop'),
                    on_click=lambda: asyncio.create_task(_cancel(plugin)),
                ).props('dense outline color=negative')

        progress_label = ui.label('').classes('text-xs font-mono mb-2')

        jobs_table = ui.table(
            columns=[
                {'name': 'started', 'label': 'Started', 'field': 'started', 'align': 'left'},
                {'name': 'kind', 'label': 'Kind', 'field': 'kind', 'align': 'left'},
                {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                {'name': 'duration', 'label': 'Duration', 'field': 'duration', 'align': 'left'},
            ],
            rows=[], row_key='id',
        ).classes('w-full border-none')

        def update():
            running = plugin.network.is_running()
            enabled = enabled_predicate(plugin)
            run_btn.set_enabled(enabled and not running)
            cancel_btn.set_visibility(running)

            if running:
                job = plugin.db.get_job(plugin.network.current_job_id())
                progress_label.text = (job or {}).get('progress') or 'Starting...'
                progress_label.style(f"color: {STATUS_COLORS['online']}")
            elif not enabled:
                progress_label.text = 'Not available — check monitor configuration'
                progress_label.style(f"color: {STATUS_COLORS['offline']}")
            else:
                progress_label.text = ''

            jobs_table.rows = [
                {
                    'id': j['id'], 'started': j['started'], 'kind': j['kind'],
                    'state': j['state'], 'duration': format_duration(j['duration']),
                }
                for j in plugin.network.recent(limit=history_limit)
            ]
            jobs_table.update()

        safe_timer(spec.get('refresh_interval', 2.0), update)


async def _start(plugin, spec: dict):
    if plugin.network.is_running():
        ui.notify('A job is already running', type='warning')
        return
    ui.notify(f"{spec.get('run_label', 'Job')} started", type='positive')
    asyncio.create_task(plugin.on_action(spec['run_action_id']))


async def _cancel(plugin):
    if await plugin.network.cancel():
        ui.notify('Cancellation requested', type='warning')
    else:
        ui.notify('No job is running', type='info')


def render_host_card(target: str):
    return info_card('TARGET HOST', target)

def render_status_card(page, collector: str, metric_name: str, title: str = 'STATUS',
                       on_text: str = 'ACTIVE', off_text: str = 'INACTIVE',
                       value_classes: str = VALUE_CLASS):
    lbl = info_card(title, 'Checking...', value_classes=value_classes)
    page.track_metric(metric_name)

    def _on_off_text(value):
        if value is None:
            return 'Checking...'
        return on_text if value > 0.5 else off_text

    lbl.bind_text_from(page.model, ('metrics', metric_name), backward=_on_off_text)

    def update_color():
        value = page.model.metrics.get(metric_name)
        if value is not None:
            is_on = value > 0.5
            lbl.style(f"color: {STATUS_COLORS['online'] if is_on else STATUS_COLORS['failed']}")

    page.on_refresh(update_color)
    return lbl
