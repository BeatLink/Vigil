from typing import Any, Callable, Dict, Optional

FORMATTERS: Dict[str, Callable[[Optional[float]], str]] = {}


def register_formatter(name: str):
    def wrap(fn):
        FORMATTERS[name] = fn
        return fn
    return wrap


def _fmt(value):
    return '--' if value is None else value


@register_formatter('int')
def _int(v):
    return '--' if v is None else str(int(v))

@register_formatter('int_rounded')
def _int_rounded(v):
    return '--' if v is None else f'{v:.0f}'

@register_formatter('count_comma')
def _count_comma(v):
    return '--' if v is None else f'{int(v):,}'

@register_formatter('count_comma_rounded')
def _count_comma_rounded(v):
    return '--' if v is None else f'{v:,.0f}'

@register_formatter('decimal1')
def _decimal1(v):
    return '--' if v is None else f'{v:.1f}'

@register_formatter('percent0')
def _percent0(v):
    return '-- %' if v is None else f'{v:.0f}%'

@register_formatter('percent0_plain_dash')
def _percent0_plain_dash(v):
    return '--' if v is None else f'{v:.0f}%'

@register_formatter('percent1')
def _percent1(v):
    return '-- %' if v is None else f'{v:.1f}%'

@register_formatter('percent1_plain_dash')
def _percent1_plain_dash(v):
    return '--' if v is None else f'{v:.1f}%'

@register_formatter('ms0')
def _ms0(v):
    return '--' if v is None else f'{v:.0f} ms'

@register_formatter('ms1')
def _ms1(v):
    return '--' if v is None else f'{v:.1f} ms'

@register_formatter('seconds_ms')
def _latency_ms(v):
    return '-- ms' if v is None else f'{v:.1f} ms'

@register_formatter('temp_c0')
def _temp_c0(v):
    return '--' if v is None else f'{v:.0f}°C'

@register_formatter('temp_c1')
def _temp_c1(v):
    return '--' if v is None else f'{v:.1f}°C'

@register_formatter('bytes_gb')
def _bytes_gb(v):
    from vigil.core.common.plugin_helpers import format_bytes
    return '--' if v is None else format_bytes(v)

@register_formatter('kbps_rate')
def _kbps_rate(v):
    if v is None:
        return '-- KB/s'
    if v >= 1024:
        return f'{v / 1024:.1f} MB/s'
    return f'{v:.1f} KB/s'

@register_formatter('dbm0')
def _dbm0(v):
    return '-- dBm' if v is None else f'{v:.0f} dBm'

@register_formatter('dedup_ratio')
def _dedup_ratio(v):
    return '--' if v is None else f'{v:.1f}x'

@register_formatter('ttl_seconds')
def _ttl_seconds(v):
    return '--' if v is None else f'{int(v)}s'


COLOR_RULES: Dict[str, Callable[[Optional[float]], Optional[str]]] = {}


def register_color_rule(name: str):
    def wrap(fn):
        COLOR_RULES[name] = fn
        return fn
    return wrap


ITEM_FORMATTERS: Dict[str, Callable[[dict], str]] = {}


def register_item_formatter(name: str):
    """Like register_formatter, but the formatter receives the whole
    item/row dict — for repeat-card text composed from more than one field
    (e.g. '42% · inodes 7%')."""
    def wrap(fn):
        ITEM_FORMATTERS[name] = fn
        return fn
    return wrap


ITEM_COLOR_RULES: Dict[str, Callable[[dict], Optional[str]]] = {}


def register_item_color_rule(name: str):
    """Like register_color_rule, but the rule receives the whole item/row
    dict rather than a single metric value — for repeat-cards and table
    cells whose color depends on more than one field."""
    def wrap(fn):
        ITEM_COLOR_RULES[name] = fn
        return fn
    return wrap


ENABLED_PREDICATES: Dict[str, Callable[[Any], bool]] = {}


def register_enabled_predicate(name: str):
    """Pure predicate over a plugin instance, used for row-action/button
    visible_if and job_panel enabled_if. Plugin-instance-specific (e.g. a
    config flag), not derivable from a single metric or item."""
    def wrap(fn):
        ENABLED_PREDICATES[name] = fn
        return fn
    return wrap


@register_color_rule('nonzero_warning')
def _nonzero_warning(v):
    if v is None:
        return None
    return 'warning' if v > 0 else 'online'


def threshold_color(warning: float, threshold: float):
    def rule(v):
        if v is None:
            return None
        from vigil.core.common.plugin_helpers import level_for
        level = level_for(v, warning, threshold)
        return {'online': 'online', 'warning': 'warning', 'failed': 'failed'}[level]
    return rule


def _dialog_spec_for(plugin: Any, dialog_name: str) -> Optional[Dict[str, Any]]:
    ui_spec = getattr(plugin, 'UI_SPEC', None) or {}
    return ui_spec.get('dialogs', {}).get(dialog_name)


def generic_render(plugin: Any, context: str = 'page', spec: Optional[Dict[str, Any]] = None,
                   page=None, start: bool = True):
    from nicegui import ui
    from vigil.web.ui.layout import PluginLayout, make_inline_layout
    from vigil.web.ui.components import (
        info_card, history_chart, render_repeat_card, render_table_with_actions,
        render_buttons, render_job_panel,
    )
    from vigil.web.ui.theme import STATUS_COLORS

    spec = spec if spec is not None else getattr(plugin, 'UI_SPEC', None)
    if spec is None:
        raise ValueError(
            f"{plugin.__class__.__name__} has no UI_SPEC and none was passed to generic_render()"
        )

    layout_rows = spec.get('layout', [])
    cards = spec.get('cards', {})
    chart_spec = spec.get('chart')
    charts = dict(spec.get('charts', {}))
    if chart_spec:
        charts.setdefault('chart', chart_spec)
    show_events = spec.get('events', False)
    show_logs = spec.get('logs', False)
    tables = spec.get('tables', {})
    filters = spec.get('filters', {})
    buttons = spec.get('buttons', {})
    job_panel_spec = spec.get('job_panel')

    layout = PluginLayout(
        plugin.config,
        layout_rows if context == 'page' else make_inline_layout(layout_rows),
    )

    if page is None:
        metric_names = [c['metric'] for name, c in cards.items()
                        if 'metric' in c and name != 'status_card']
        for c in cards.values():
            metric_names += c.get('metrics', [])
        metric_names += [c['metric'] for c in charts.values()]
        page = plugin.ui.page(metric_names=metric_names)

    color_updates = []

    for widget_name, card_spec in cards.items():
        if widget_name == 'host_card' or widget_name == 'status_card':
            continue

        if 'repeat' in card_spec:
            with layout.cell(widget_name):
                render_repeat_card(plugin, page, card_spec['repeat'])
            continue

        title = card_spec['title']

        if 'metric' not in card_spec:
            if 'value_attr' in card_spec:
                value = getattr(plugin, card_spec['value_attr'])
                text = card_spec.get('value_format', '{}').format(value)
            else:
                text = card_spec.get('value', '--')
            with layout.cell(widget_name):
                label = info_card(title, text)

            color_attr = card_spec.get('color_attr')
            if color_attr or card_spec.get('refresh'):
                def _make_attr_update(lbl=label, value_attr=card_spec.get('value_attr'),
                                      value_format=card_spec.get('value_format', '{}'),
                                      color_attr=color_attr):
                    def _update():
                        if value_attr:
                            lbl.text = value_format.format(getattr(plugin, value_attr))
                        if color_attr:
                            state = getattr(plugin, color_attr)
                            if state is not None:
                                lbl.style(f'color: {STATUS_COLORS[state]}')
                    return _update
                color_updates.append(_make_attr_update())
            continue

        if 'metrics' in card_spec:
            metric_list = card_spec['metrics']
            format_fn = ITEM_FORMATTERS[card_spec['format_fn']]
            color_fn_name = card_spec.get('color_fn')
            color_fn = ITEM_FORMATTERS.get(color_fn_name) if color_fn_name else None
            with layout.cell(widget_name):
                label = info_card(title, format_fn({}))

            def _make_multi_update(lbl=label, metric_list=metric_list, format_fn=format_fn, color_fn=color_fn):
                def _update():
                    values = {m: page.model.metrics.get(m) for m in metric_list}
                    lbl.text = format_fn(values)
                    if color_fn:
                        state = color_fn(values)
                        if state is not None:
                            lbl.style(f'color: {STATUS_COLORS[state]}')
                return _update
            color_updates.append(_make_multi_update())
            continue

        metric_name = card_spec['metric']
        fmt_name = card_spec.get('format', 'int')
        formatter = FORMATTERS.get(fmt_name)
        if formatter is None:
            raise KeyError(
                f"UI_SPEC card {widget_name!r} references unknown format {fmt_name!r} "
                f"— register it via spec.register_formatter first"
            )
        default_text = formatter(None)
        with layout.cell(widget_name):
            label = info_card(title, default_text).bind_text_from(
                page.model, ('metrics', metric_name), backward=formatter)

        color_name = card_spec.get('color')
        if color_name:
            color_rule = COLOR_RULES.get(color_name)
            if color_rule is None:
                raise KeyError(
                    f"UI_SPEC card {widget_name!r} references unknown color rule {color_name!r} "
                    f"— register it via spec.register_color_rule first"
                )
            def _make_update(lbl=label, metric=metric_name, rule=color_rule):
                def _update():
                    value = page.model.metrics.get(metric)
                    state = rule(value)
                    if state is not None:
                        lbl.style(f'color: {STATUS_COLORS[state]}')
                return _update
            color_updates.append(_make_update())

    if 'host_card' in _flatten_layout(layout_rows):
        with layout.cell('host_card'):
            plugin.ui.host_card()

    if 'status_card' in cards:
        sc = cards['status_card']
        with layout.cell('status_card'):
            plugin.ui.status_card(
                page,
                metric_name=sc['metric'],
                title=sc.get('title', 'STATUS'),
                on_text=sc.get('on_text', 'ACTIVE'),
                off_text=sc.get('off_text', 'INACTIVE'),
            )

    for widget_name, cs in charts.items():
        with layout.cell(widget_name):
            history_chart(page, cs.get('title', cs['metric'].upper()),
                          plugin.id, cs['metric'])

    dynamic_charts_spec = spec.get('dynamic_charts')
    if dynamic_charts_spec:
        with layout.cell(dynamic_charts_spec.get('widget', 'charts')):
            for chart_title, chart_metric in getattr(plugin, dynamic_charts_spec['items_attr']):
                history_chart(page, chart_title, plugin.id, chart_metric)

    if show_events:
        events_kwargs = show_events if isinstance(show_events, dict) else {}
        with layout.cell('events'):
            plugin.ui.events_table(page, **events_kwargs)

    if show_logs:
        logs_kwargs = show_logs if isinstance(show_logs, dict) else {}
        with layout.cell('logs'):
            plugin.ui.logs_table(page, **logs_kwargs)

    for widget_name, table_spec in tables.items():
        with layout.cell(widget_name):
            render_table_with_actions(plugin, page, table_spec, filters.get(widget_name))

    for widget_name, button_specs in buttons.items():
        with layout.cell(widget_name):
            render_buttons(plugin, button_specs)

    if job_panel_spec:
        with layout.cell(job_panel_spec.get('widget', 'jobs')):
            render_job_panel(plugin, job_panel_spec)

    if color_updates:
        def _update_all_colors():
            for update in color_updates:
                update()
        page.on_refresh(_update_all_colors)

    if start:
        page.start()

    return page


def _flatten_layout(rows):
    for row in rows:
        for item in row:
            yield item if isinstance(item, str) else item['widget']
