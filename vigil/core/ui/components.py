"""Reusable NiceGUI building blocks shared by the UI_SPEC renderer and plugin views."""
import asyncio
from typing import Optional
from nicegui import ui
from vigil.core.contracts import RefreshCallback
from .theme import STATUS_COLORS


def offload(read_fn):
    """Wrap a blocking store read as a coroutine that runs it on the default executor."""
    async def _run(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: read_fn(*args, **kwargs))
    return _run


def refresh_rows(table, new_rows) -> None:
    """Replace a table's rows only when they changed, so unchanged tables send nothing."""
    if new_rows != table.rows:
        table.rows = new_rows
        table.update()


def on_data_event(callback: RefreshCallback, run_now: bool = True):
    """Refresh on push: register `callback` on the current client's shared
    scheduler, which the Database Engine's change bus wakes whenever anything
    is written. `run_now=False` defers the first tick to the next scheduler
    tick instead of firing inline during construction."""
    from vigil.core.ui.model import schedule_callback
    schedule_callback(callback, run_now=run_now)


LABEL_CLASS = 'halon-label'
VALUE_CLASS = 'halon-value'
SECTION_CLASS = 'halon-title-section'
HOVER_CLASS = 'halon-row-hover cursor-pointer'

def card(classes: str = '', padding: bool = True):
    """A raised panel: the surface content sits on, delimited by the card
    hairline rather than by a shadow-heavy elevation."""
    p = 'halon-card' if padding else 'halon-card halon-card--flush'
    return ui.card().classes(f'{p} {classes}')

def info_card(title: str, value: str = '--', value_classes: str = VALUE_CLASS, card_classes: str = 'flex-1 min-w-[9rem]'):
    """Render a small labeled-value card and return the value label for later binding."""
    with card(f'h-24 overflow-hidden items-center justify-center gap-1 {card_classes}'):
        ui.label(title).classes(LABEL_CLASS)
        # A value is a heading, and headings are never accent-colored (§6.8);
        # only a status rule may recolor one, and then to a status token.
        return ui.label(value).classes(f'{value_classes} w-full text-center break-words')

def action_button(text: str, on_click=None, icon: str = 'play_arrow',
                  weight: str = 'default', danger: bool = False):
    """One of the three button weights of §6.1: 'filled' for the single
    primary action on a view, 'default' for ordinary ones, 'flat' for anything
    repeated. Destructive actions swap the accent for the danger token."""
    classes = {'filled': 'halon-button-filled', 'flat': 'halon-button-flat'}.get(weight, 'halon-button')
    if danger:
        classes += ' halon-button-danger'
    # color=None keeps Quasar from adding bg-primary/text-white, whose
    # !important cascade layer outranks any author rule (§8, framework layer).
    return ui.button(text, icon=icon, on_click=on_click, color=None).props('no-caps').classes(classes)

def section_title(text: str, classes: str = ''):
    """Render a section heading in the shared title style."""
    return ui.label(text).classes(f'{SECTION_CLASS} mb-6 {classes}')

def feed_columns(target_label: Optional[str] = None, sortable: tuple = (),
                 message_classes: str = '') -> list:
    """Build the shared time/level/message column set, optionally with a target column."""
    columns = [
        {'name': 'timestamp', 'label': 'Time', 'field': 'timestamp', 'align': 'left',
         'sortable': 'timestamp' in sortable},
        {'name': 'level', 'label': 'Level', 'field': 'level', 'align': 'left',
         'sortable': 'level' in sortable},
    ]
    if target_label:
        columns.append({'name': 'target', 'label': target_label, 'field': 'target',
                        'align': 'left', 'sortable': 'target' in sortable})
    columns.append({'name': 'message', 'label': 'Message', 'field': 'message',
                    'align': 'left', 'classes': message_classes})
    return columns


def _feed_table(page, read, title: str, full_height: bool):
    """A time/level/message table over any store read; the log and event
    tables differ only in what they read."""
    card_classes = 'w-full overflow-hidden flex-grow' if full_height else ''

    with card(card_classes, padding=not full_height):
        if full_height:
            ui.label(title).classes('halon-card-header w-full')
        else:
            ui.label(title).classes(f'{LABEL_CLASS} mb-2')

        columns = feed_columns(
            sortable=('timestamp',),
            message_classes='text-wrap font-mono' if full_height else '')
        table_classes = 'w-full border-none' + (' h-[600px]' if full_height else '')
        table = ui.table(columns=columns, rows=[]).classes(table_classes)
        if full_height:
            table.props('virtual-scroll')

        async def update():
            refresh_rows(table, await offload(read)())

        page.on_refresh(update)
        return table

def log_table(page, target: str, filter_prefix: str = '', title: str = 'Recent Logs',
             limit: int = 15, full_height: bool = False):
    """Render a feed table over the target's recent log lines."""
    return _feed_table(
        page, lambda: page.plugin.data.log_lines(target, filter_prefix, limit=limit),
        title, full_height)

def event_table(page, plugin_name: str, plugin_id: str = '', target: str = '',
                title: str = 'Recent Events', limit: int = 100,
                full_height: bool = False):
    """Render a feed table over the plugin's recent events."""
    prefix = f"[{plugin_name}] "
    return _feed_table(
        page, lambda: page.plugin.data.plugin_events(plugin_id, prefix, target, limit=limit),
        title, full_height)


def history_chart(page, title: str, plugin_id: str, metric_name: str, limit: int = 30):
    """Render a line chart of one metric's recent history, repainted on push and scheme flips."""
    from vigil.core.ui import theme

    with card('w-full h-80 mb-4'):
        ui.label(title).classes(f'{LABEL_CLASS} mb-1')
        chart = ui.echart({
            'tooltip': {'trigger': 'axis'},
            'grid': {'left': 4, 'right': 8, 'top': 8, 'bottom': 4, 'containLabel': True},
            'xAxis': {'type': 'category', 'data': []},
            'yAxis': {'type': 'value', 'splitLine': {'show': False}},
            'series': [{
                'data': [],
                'type': 'line',
                'smooth': True,
                'areaStyle': {'opacity': 0.1},
            }]
        }).classes('w-full h-64')

        def _repaint(colors):
            # ECharts paints to a canvas and cannot resolve the tokens, so the
            # axes and the line are recolored whenever the client's scheme flips.
            chart.options['series'][0]['color'] = colors['accent']
            for axis in ('xAxis', 'yAxis'):
                chart.options[axis]['axisLine'] = {'lineStyle': {'color': colors['border']}}
                chart.options[axis]['axisLabel'] = {'color': colors['text_secondary']}
            chart.update()

        theme.on_scheme_change(_repaint)

        def _read():
            history = page.plugin.data.metric_history(plugin_id, metric_name, limit=limit)
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
    """A status *region*: tinted, not filled, so its text stays ordinary body
    text (§6.7). `color` is a status token, composited at 15% by halon.css."""
    style = f'--halon-tint-color: {color}' if color else ''
    return ui.label(f'{label}: {value}' if label else value).classes('halon-tint').style(style)


def _resolve_repeat_items(plugin, repeat_spec: dict) -> list:
    source = repeat_spec.get('source', 'snapshot')

    if source == 'setting':
        import json
        key = repeat_spec.get('setting_key', '').format(plugin_id=plugin.id)
        raw = plugin.data.get_setting(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        # A list of plain strings (e.g. DNS answers) becomes {'label': ..., 'value': ...}
        # items so the same item_label/item_value machinery applies uniformly.
        return [
            item if isinstance(item, dict) else {'label': item, 'value': item}
            for item in data
        ]

    if source == 'metrics_prefix':
        return _resolve_metrics_prefix_items(plugin, repeat_spec)

    return plugin.data.latest_snapshot(default=[])


def _scan_metric_family(plugin, prefix: str, suffix: str, exclude: set) -> dict:
    """Returns {stripped_key: latest_value} for metric names matching prefix/suffix."""
    latest: dict = {}
    for record in plugin.data.latest_collector_metrics():
        name = record.metric_name
        if name in exclude:
            continue
        if prefix and not name.startswith(prefix):
            continue
        if suffix and not name.endswith(suffix):
            continue
        key = name
        if prefix:
            key = key.removeprefix(prefix)
        if suffix:
            key = key.removesuffix(suffix)
        latest[key] = record.value
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

    fields = repeat_spec.get('fields')
    if fields:
        merged: dict = {}
        for field in fields:
            family = _scan_metric_family(
                plugin, field.get('prefix', ''), field.get('suffix', ''), exclude)
            for key, value in family.items():
                merged.setdefault(key, {'key': key})[field['name']] = value
        items = list(merged.values())
    else:
        family = _scan_metric_family(
            plugin, repeat_spec.get('metrics_prefix', ''),
            repeat_spec.get('metrics_suffix', ''), exclude)
        items = [{'key': key, 'value': value} for key, value in family.items()]

    items.sort(key=lambda i: i['key'])
    return items


_LABEL_TRANSFORMS = {
    'slashes': lambda s: '/' + s.replace('_', '/') if s != 'root' else '/',
    'spaces_upper': lambda s: s.replace('_', ' ').upper(),
    'none': lambda s: s,
}


def render_repeat_card(plugin, page, repeat_spec: dict):
    """Render one chip or card per item resolved from a repeat spec's data source."""
    from vigil.core.ui.spec import FORMATTERS, ITEM_COLOR_RULES, ITEM_FORMATTERS, resolve
    from vigil.core.ui.theme import STATUS_COLORS

    source = repeat_spec.get('source', 'snapshot')
    container_kind = repeat_spec.get('container', 'chips')
    default_label = 'key' if source == 'metrics_prefix' else 'label'
    default_value = 'value'
    item_label = repeat_spec.get('item_label', default_label)
    item_value = repeat_spec.get('item_value', default_value)
    item_format = repeat_spec.get('item_format')
    formatter = resolve(FORMATTERS, item_format)
    item_format_fn_name = repeat_spec.get('item_format_fn')
    item_format_fn = resolve(ITEM_FORMATTERS, item_format_fn_name)
    label_transform = _LABEL_TRANSFORMS.get(repeat_spec.get('label_transform', 'none'))
    label_prefix = repeat_spec.get('item_label_prefix', '')
    label_suffix = repeat_spec.get('item_label_suffix', '')
    color_rule_name = repeat_spec.get('item_color_by')
    color_rule = resolve(ITEM_COLOR_RULES, color_rule_name)
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
                ui.label(empty_text).classes('halon-caption')
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
    """Render a row of action buttons from button specs, honoring their visibility predicates."""
    from vigil.core.ui.spec import ENABLED_PREDICATES, resolve

    with ui.row().classes('gap-2 items-center'):
        for spec in button_specs:
            predicate_name = spec.get('visible_if')
            if predicate_name:
                predicate = resolve(ENABLED_PREDICATES, predicate_name)
                if predicate is not None and not predicate(plugin):
                    continue

            async def _click(_e=None, s=spec):
                if s.get('kind') == 'dialog':
                    await open_dialog_impl(plugin, s['dialog'])
                    return
                success, _ = await plugin.run_action(s['id'])
                if s.get('notify', True):
                    label = s.get('label', s['id'])
                    ui.notify(
                        f'{label} {"succeeded" if success else "failed"}',
                        type='positive' if success else 'negative',
                    )

            action_button(
                spec.get('label', spec['id']), icon=spec.get('icon'),
                weight='flat' if spec.get('flat', True) else 'default',
                danger=spec.get('color') in ('negative', 'danger'),
                on_click=lambda e, c=_click: asyncio.create_task(c(e)),
            )


def _substitute(template: str, row: Optional[dict], plugin) -> str:
    class _PluginProxy(dict):
        def __missing__(self, key):
            return getattr(plugin, key, '')
    return template.format(row=row or {}, plugin=_PluginProxy())


async def open_dialog_impl(plugin, dialog_name: str, row: Optional[dict] = None):
    """Open a named read or edit dialog from the plugin's dialog specs."""
    from vigil.core.ui.spec import _dialog_spec_for
    spec = _dialog_spec_for(plugin, dialog_name)
    if spec is None:
        ui.notify(f'Unknown dialog {dialog_name!r}', type='negative')
        return

    title = _substitute(spec.get('title', dialog_name), row, plugin)

    def _resolve_params(params_spec: dict) -> dict:
        return {kwarg: (row or {}).get(field) for kwarg, field in (params_spec or {}).items()}

    if spec['kind'] == 'read':
        ok, content = await plugin.run_action(spec['action_id'], **_resolve_params(spec.get('params')))
        if not ok:
            ui.notify(content or 'Action failed', type='negative')
            return
        with ui.dialog() as dialog, card('w-full'):
            ui.label(title).classes('halon-title-section mb-4')
            if spec.get('render') == 'textarea_readonly':
                ui.textarea(content).props('readonly autogrow outlined').classes('w-full')
            else:
                ui.label(content).classes('font-mono halon-caption').style('white-space: pre-wrap;')
            action_button('Close', on_click=dialog.close, icon=None, weight='flat')
        dialog.open()
        return

    if spec['kind'] == 'edit':
        ok, content = await plugin.run_action(
            spec['load_action_id'], **_resolve_params(spec.get('load_params')))
        if not ok:
            ui.notify(content or 'Unable to load content', type='negative')
            return
        with ui.dialog() as dialog, card('w-full'):
            ui.label(title).classes('halon-title-section mb-4')
            editor = ui.textarea(content).props('autogrow outlined').classes('w-full h-96')
            with ui.row().classes('justify-end gap-2 mt-4'):
                action_button('Cancel', on_click=dialog.close, icon=None, weight='flat')

                async def save():
                    save_kwargs = _resolve_params(spec.get('save_params'))
                    save_kwargs[spec.get('save_content_kwarg', 'content')] = editor.value
                    save_ok, _ = await plugin.run_action(spec['save_action_id'], **save_kwargs)
                    ui.notify(
                        spec.get('success_message', 'Saved') if save_ok else 'Save failed',
                        type='positive' if save_ok else 'negative',
                    )
                    if save_ok:
                        dialog.close()

                action_button('Save', on_click=save, icon=None, weight='filled')
        dialog.open()
        return


def render_table_with_actions(plugin, page, table_spec: dict, filter_spec: Optional[dict] = None):
    """Render a data table with optional per-row action buttons, cell coloring and filtering."""
    from vigil.core.ui.spec import ENABLED_PREDICATES, ITEM_COLOR_RULES, resolve
    from vigil.core.ui.theme import STATUS_COLORS

    row_key = table_spec.get('row_key', 'id')
    columns = list(table_spec.get('columns', []))
    row_actions = [
        a for a in table_spec.get('row_actions', [])
        if not a.get('visible_if') or (resolve(ENABLED_PREDICATES, a['visible_if']) or (lambda p: True))(plugin)
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

    table = ui.table(columns=render_columns, rows=[], row_key=row_key).classes('w-full')

    for col in columns:
        color_rule_name = col.get('cell_color_by')
        if not color_rule_name:
            continue
        rule = resolve(ITEM_COLOR_RULES, color_rule_name)
        if rule is None:
            continue
        table.add_slot(f'body-cell-{col["name"]}', f'''
            <q-td :props="props">
                <span :style="{{ color: props.row._color_{col['name']} }}">{{{{ props.row.{col['field']} }}}}</span>
            </q-td>
        ''')

    if row_actions:
        # Row buttons repeat once per row, so they take the flat weight: a
        # secondary glyph that goes accent on hover. Only a destructive one
        # spends a color, and it spends the danger token (§6.1).
        buttons_html = ''.join(
            f'''<q-btn dense flat no-caps icon="{a['icon']}" size="sm"
                       class="{'halon-button-danger' if a.get('color') == 'negative' else ''}"
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
        action_id = action.get('action_id', action['id'])
        success, _ = await plugin.run_action(action_id, **params)
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
            rule = resolve(ITEM_COLOR_RULES, color_rule_name)
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
    """Render the run/cancel controls, progress line and history table for a plugin's jobs."""
    from vigil.core.ui.spec import ENABLED_PREDICATES, resolve
    from vigil.core.ui.theme import STATUS_COLORS
    from vigil.plugins.base.plugin_helpers import format_duration

    history_limit = spec.get('history_limit', 10)
    enabled_name = spec.get('enabled_if')
    enabled_predicate = (resolve(ENABLED_PREDICATES, enabled_name) or (lambda p: True)) if enabled_name else (lambda p: True)

    with card('w-full'):
        with ui.row().classes('w-full items-center justify-between mb-2'):
            ui.label(spec.get('title', 'Jobs')).classes(LABEL_CLASS)
            with ui.row().classes('gap-2'):
                run_btn = action_button(
                    spec.get('run_label', 'Run'), icon=spec.get('run_icon', 'play_arrow'),
                    weight='filled',
                    on_click=lambda: asyncio.create_task(_start(plugin, spec)),
                )
                cancel_btn = action_button(
                    spec.get('cancel_label', 'Cancel'), icon=spec.get('cancel_icon', 'stop'),
                    danger=True,
                    on_click=lambda: asyncio.create_task(_cancel(plugin)),
                )

        progress_label = ui.label('').classes('halon-caption font-mono mb-2')

        jobs_table = ui.table(
            columns=[
                {'name': 'started', 'label': 'Started', 'field': 'started', 'align': 'left'},
                {'name': 'kind', 'label': 'Kind', 'field': 'kind', 'align': 'left'},
                {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                {'name': 'duration', 'label': 'Duration', 'field': 'duration', 'align': 'left'},
            ],
            rows=[], row_key='id',
        ).classes('w-full border-none')

        last_progress_color = [None]

        def update():
            running = plugin.jobs.is_running()
            enabled = enabled_predicate(plugin)
            run_btn.set_enabled(enabled and not running)
            cancel_btn.set_visibility(running)

            if running:
                job = plugin.data.job(plugin.jobs.current_id())
                progress_label.text = (job or {}).get('progress') or 'Starting...'
                color = STATUS_COLORS['online']
            elif not enabled:
                progress_label.text = 'Not available — check monitor configuration'
                color = STATUS_COLORS['offline']
            else:
                progress_label.text = ''
                color = None
            if color is not None and color != last_progress_color[0]:
                last_progress_color[0] = color
                progress_label.style(f"color: {color}")

            refresh_rows(jobs_table, [
                {
                    'id': j['id'], 'started': j['started'], 'kind': j['kind'],
                    'state': j['state'], 'duration': format_duration(j['duration']),
                }
                for j in plugin.jobs.recent(limit=history_limit)
            ])

        on_data_event(update)


async def _start(plugin, spec: dict):
    if plugin.jobs.is_running():
        ui.notify('A job is already running', type='warning')
        return
    ui.notify(f"{spec.get('run_label', 'Job')} started", type='positive')
    asyncio.create_task(plugin.run_action(spec['run_action_id']))


async def _cancel(plugin):
    if await plugin.jobs.cancel():
        ui.notify('Cancellation requested', type='warning')
    else:
        ui.notify('No job is running', type='info')


def render_host_card(target: str):
    """Render the standard card naming the monitored host."""
    return info_card('TARGET HOST', target)

def render_status_card(page, plugin_id: str, metric_name: str, title: str = 'STATUS',
                       on_text: str = 'ACTIVE', off_text: str = 'INACTIVE',
                       value_classes: str = VALUE_CLASS):
    """Render an on/off status card bound to a 0-or-1 metric."""
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
