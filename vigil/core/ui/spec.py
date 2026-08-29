"""Renderer and reference for the declarative UI_SPEC dicts most plugins use in place of a
hand-written render_ui().

A plugin exposes a ``UI_SPEC`` dict (usually a ``@property``) and ``generic_render()`` walks
it to build the page: a grid of layout cells filled with info cards, history charts, tables
with row actions, buttons, dialogs, and a job panel. ``spec_types.py`` holds the TypedDict
shapes; this docstring is the prose contract a plugin author writes against.

Function references — name or callable
======================================
Anywhere a spec field references a function (``format``, ``color``, ``format_fn``,
``color_fn``, ``item_format_fn``, ``item_color_by``, ``cell_color_by``, ``visible_if``,
``enabled_if``), the value may be either the NAME of a registered
function or the CALLABLE itself — ``resolve()`` accepts both. The shared vocabulary below is
name-keyed so specs stay plain data; a one-off plugin transform passes its bound method
directly. A plugin that needs an instance-tuned rule (e.g. ``threshold_color()`` curried
with its config thresholds) registers it under a unique per-instance name in ``__init__``.

Registries
==========
=================== ================================== ============================
registry            callable shape                     registered via
=================== ================================== ============================
FORMATTERS          Optional[float] -> str             register_formatter()
COLOR_RULES         Optional[float] -> Optional[name]  register_color_rule()
ITEM_FORMATTERS     item dict -> str                   register_item_formatter()
ITEM_COLOR_RULES    item dict -> Optional[name]        register_item_color_rule()
ENABLED_PREDICATES  plugin -> bool                     register_enabled_predicate()
=================== ================================== ============================

A color rule returns a status name (``'online'`` | ``'warning'`` | ``'failed'``, a
``theme.STATUS_COLORS`` key) or None for "leave the color unchanged". Only FORMATTERS and
COLOR_RULES ship shared entries (tables below); the other three start empty and are filled
by plugins, or bypassed entirely by passing callables.

Top-level keys (each optional — a missing key means "not used")
===============================================================
layout          List of rows; each row lists widget names, as bare strings or as
                ``{'widget': name}`` plus ``visible``/``height``/``flex``/``min_width``/
                ``title`` cell overrides. Every other key renders into the layout cell of
                the same widget name; a widget with no cell renders hidden.
                ``plugin_config['layout']`` may replace the rows wholesale (a list) or
                override per-widget cell properties (a dict).
cards           ``{widget_name: card spec}`` — see "Card specs". ``host_card`` and
                ``status_card`` are reserved names with fixed renderings.
chart           Single-chart shorthand for ``charts={'chart': ...}``.
charts          ``{widget_name: {'metric': ..., 'title': ...}}`` history charts; the title
                defaults to the metric name upper-cased. A plugin whose charts depend on
                its config builds this dict (and the matching layout rows) in its
                ``UI_SPEC`` property — see ports.py.
events          True, or a kwargs dict forwarded to the plugin events table (e.g.
                ``{'title': 'PLUGIN EVENTS'}``); renders into the ``events`` cell.
tables          ``{widget_name: table spec}`` — see "Table specs".
filters         ``{widget_name: {'placeholder': ..., 'fields': [row keys]}}`` — a text
                filter attached to the table with the same widget name.
buttons         ``{widget_name: [button spec, ...]}`` — one row of action buttons per cell.
                A button spec: ``{'id', 'label', 'icon', 'color', 'flat' (default True),
                'visible_if' (ENABLED_PREDICATES), 'kind', 'dialog', 'notify' (default
                True)}``; kind ``'dialog'`` opens the named dialog, anything else awaits
                ``plugin.run_action(id)`` and notifies the outcome.
dialogs         ``{dialog_name: dialog spec}`` — opened by kind ``'dialog'`` buttons and
                row actions; see "Dialog specs".
job_panel       ``{'widget' (default 'jobs'), 'title', 'run_label', 'run_icon',
                'cancel_label', 'cancel_icon', 'enabled_if' (ENABLED_PREDICATES),
                'run_action_id', 'history_limit'}`` — a run/cancel panel with history for
                one long-running job.

``generic_render(context='inline')`` renders the same spec inside another page with the
host card hidden; a group plugin passes ``layout=`` a view onto its own grid instead.

Card specs
==========
Every card has a ``title`` plus one value source; generic_render dispatches on the first
match, in this order:

``repeat``      A family of dynamically discovered items rendered as chips or mini-cards
                in place of a single value — see "Repeat specs".
no ``metric``   ``value_attr``: ``getattr(plugin, value_attr)`` pushed through the
key present:    ``value_format`` template (default ``'{}'``); otherwise static ``value``
                text (default ``'--'``). With ``color_attr`` (a plugin attribute holding a
                status name) and/or ``refresh: True`` the card re-reads on every page
                refresh; otherwise it renders once.
``metrics``     Several metrics combined into one card: ``format_fn`` (required) maps
                ``{metric: value}`` to the text and ``color_fn`` maps the same dict to a
                status name; both are item-level functions (name or callable).
``metric``      One metric bound live: ``format`` names a FORMATTERS entry (default
                ``'int'``) and ``color`` names a COLOR_RULES entry applied on refresh.

Dispatch precedence when a card carries several of these: ``metrics``, then
``metric``, then the static/attribute forms.

Reserved cards:

host_card       The shared host summary card; no other keys are read.
status_card     ``{'metric', 'title' (default 'STATUS'), 'on_text' (default 'ACTIVE'),
                'off_text' (default 'INACTIVE')}`` — shows on_text colored online when the
                metric is > 0.5, off_text colored failed otherwise, ``Checking...`` while
                the metric is absent.

Repeat specs (cards[name]['repeat'])
====================================
source            ``'snapshot'`` (default): the plugin's latest snapshot list;
                  ``'setting'``: a JSON list from the settings store under ``setting_key``
                  (``'{plugin_id}'`` is substituted), plain strings becoming label/value
                  items; ``'metrics_prefix'``: items discovered from metric names.
container         ``'chips'`` (default) for label: value chips, anything else for a grid
                  of small info cards.
item_label        Item key holding the label (``'key'`` for metrics_prefix, else
                  ``'label'``).
item_value        Item key holding the value (default ``'value'``).
item_format       FORMATTERS name applied to the value.
item_format_fn    ITEM_FORMATTERS name/callable over the whole item; takes precedence
                  over item_format.
label_transform   ``'none'`` (default) | ``'slashes'`` (``'root'`` → ``/``, ``'var_log'``
                  → ``/var/log``) | ``'spaces_upper'`` (``'instance_id'`` → ``INSTANCE
                  ID``).
item_label_prefix / item_label_suffix   Literal text wrapped around each label.
item_color_by     ITEM_COLOR_RULES name/callable over the whole item.
empty_text        Shown when there are no items (default ``'No data'``).
metrics_prefix / metrics_suffix / metrics_exclude   For source ``'metrics_prefix'``:
                  match metric names, strip the affixes, and emit one ``{'key', 'value'}``
                  item per remaining metric.
fields            ``[{'name', 'prefix', 'suffix'}, ...]`` merges several metric families
                  sharing a stripped key into one item dict (``fs_<k>_used_pct`` +
                  ``fs_<k>_inodes_pct`` → ``{'key', 'used_pct', 'inodes_pct'}``).

Table specs (tables[widget_name])
=================================
row_key         Unique row field (default ``'id'``).
columns         ``[{'name', 'label', 'field', 'align', 'sortable', 'cell_color_by'
                (ITEM_COLOR_RULES name/callable over the row)}, ...]``.
row_actions     ``[{'id', 'icon', 'color', 'tooltip', 'visible_if' (ENABLED_PREDICATES),
                'kind', 'dialog', 'action_id' (default id), 'params' (kwarg name -> row
                field), 'notify' (default True)}, ...]``; kind ``'dialog'`` opens the
                named dialog with the row, anything else awaits
                ``plugin.run_action(action_id, **params)``.
rows_attr       ``getattr(plugin, rows_attr)`` supplies the rows.
source          Repeat-spec source used when rows_attr is absent (default ``'snapshot'``).

Dialog specs (dialogs[dialog_name])
===================================
``title`` is a template; ``{row[field]}`` and ``{plugin.attr}`` are substituted.

kind 'read'     Runs ``action_id`` with ``params`` (kwarg -> row field) and shows the
                returned content; ``render: 'textarea_readonly'`` gives a copyable text
                area, anything else a preformatted label.
kind 'edit'     Loads content via ``load_action_id``/``load_params`` into an editor, saves
                via ``save_action_id``/``save_params`` with the edited text passed under
                ``save_content_kwarg`` (default ``'content'``), and notifies
                ``success_message`` on success.

Formatters (FORMATTERS)
=======================
Each maps one Optional[float] metric value to display text; None means "no data yet".

===================== ============================================== ==========
name                  example                                        None
===================== ============================================== ==========
int                   87.6 → '87'                                    '--'
int_rounded           87.6 → '88'                                    '--'
count_comma           1234567.9 → '1,234,567'                        '--'
count_comma_rounded   1234567.9 → '1,234,568'                        '--'
decimal1              3.14 → '3.1'                                   '--'
percent0              87.4 → '87%'                                   '-- %'
percent0_plain_dash   87.4 → '87%'                                   '--'
percent1              87.46 → '87.5%'                                '-- %'
percent1_plain_dash   87.46 → '87.5%'                                '--'
ms0                   12.7 → '13 ms'                                 '--'
ms1                   12.66 → '12.7 ms'                              '--'
seconds_ms            12.66 → '12.7 ms'                              '-- ms'
temp_c0               56.7 → '57°C'                                  '--'
temp_c1               56.74 → '56.7°C'                               '--'
bytes_gb              0.5 → '512 MB', 12.34 → '12.3 GB',             '--'
(value in GB)         2048.0 → '2.0 TB'
kbps_rate             512.0 → '512.0 KB/s', 2048.0 → '2.0 MB/s'      '-- KB/s'
(value in KB/s)
dbm0                  -67.4 → '-67 dBm'                              '-- dBm'
dedup_ratio           3.27 → '3.3x'                                  '--'
ttl_seconds           300.0 → '300s'                                 '--'
===================== ============================================== ==========

Color rules (COLOR_RULES)
=========================
================= =================================================
name              example
================= =================================================
nonzero_warning   0 → 'online', 3 → 'warning', None → None
nonzero_failed    0 → 'online', 3 → 'failed', None → None
always_online     87.6 → 'online', 0 → 'online', None → None
================= =================================================

``threshold_color(warning, threshold)`` builds the standard banded rule — value >=
threshold → 'failed', value >= warning → 'warning', else 'online', None → None — for a
plugin to register under a per-instance name with its config thresholds.
"""

from typing import Any, Dict, List, Optional

from vigil.core.ui.spec_types import (
    ColorRule, EnabledPredicate, Formatter, ItemColorRule, ItemFormatter, UISpec,
)

def resolve(registry: Dict[str, Any], ref, required: bool = False):
    """A spec entry may name a registered function or hold the callable
    itself; shared vocabulary stays name-keyed, one-off plugin transforms
    pass their method directly."""
    if ref is None or callable(ref):
        return ref
    fn = registry.get(ref)
    if required and fn is None:
        raise KeyError(f"unknown spec function {ref!r}")
    return fn


FORMATTERS: Dict[str, Formatter] = {}


def register_formatter(name: str):
    """Register a value formatter under a spec-referencable name."""
    def wrap(fn):
        FORMATTERS[name] = fn
        return fn
    return wrap


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
    from vigil.plugins.base.plugin_helpers import format_bytes
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


COLOR_RULES: Dict[str, ColorRule] = {}


def register_color_rule(name: str):
    """Register a color rule under a spec-referencable name."""
    def wrap(fn):
        COLOR_RULES[name] = fn
        return fn
    return wrap


ITEM_FORMATTERS: Dict[str, ItemFormatter] = {}


def register_item_formatter(name: str):
    """Like register_formatter, but the formatter receives the whole
    item/row dict — for repeat-card text composed from more than one field
    (e.g. '42% · inodes 7%')."""
    def wrap(fn):
        ITEM_FORMATTERS[name] = fn
        return fn
    return wrap


ITEM_COLOR_RULES: Dict[str, ItemColorRule] = {}


def register_item_color_rule(name: str):
    """Like register_color_rule, but the rule receives the whole item/row
    dict rather than a single metric value — for repeat-cards and table
    cells whose color depends on more than one field."""
    def wrap(fn):
        ITEM_COLOR_RULES[name] = fn
        return fn
    return wrap


ENABLED_PREDICATES: Dict[str, EnabledPredicate] = {}


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


@register_color_rule('always_online')
def _always_online(v):
    return None if v is None else 'online'


@register_color_rule('nonzero_failed')
def _nonzero_failed(v):
    if v is None:
        return None
    return 'failed' if v > 0 else 'online'


def threshold_color(warning: float, threshold: float):
    """Build a color rule that maps a value onto status colors by two thresholds."""
    def rule(v):
        if v is None:
            return None
        from vigil.plugins.base.plugin_helpers import level_for
        level = level_for(v, warning, threshold)
        return {'online': 'online', 'warning': 'warning', 'failed': 'failed'}[level]
    return rule


def _dialog_spec_for(plugin: Any, dialog_name: str) -> Optional[Dict[str, Any]]:
    ui_spec: UISpec = getattr(plugin, 'UI_SPEC', None) or {}
    return ui_spec.get('dialogs', {}).get(dialog_name)


def _make_attr_update(plugin: Any, label, value_attr: Optional[str],
                      value_format: str, color_attr: Optional[str]):
    """Build a refresh callback re-reading plugin attributes into a value card, restyling only when the color state changes."""
    from vigil.core.ui.theme import STATUS_COLORS
    last_state = [None]

    def _update():
        if value_attr:
            label.text = value_format.format(getattr(plugin, value_attr))
        if color_attr:
            state = getattr(plugin, color_attr)
            if state is not None and state != last_state[0]:
                last_state[0] = state
                label.style(f'color: {STATUS_COLORS[state]}')
    return _update


def _make_multi_update(page, label, metric_list: List[str], format_fn, color_fn):
    """Build a refresh callback recomputing a multi-metric card's text and color, restyling only when the color state changes."""
    from vigil.core.ui.theme import STATUS_COLORS
    last_state = [None]

    def _update():
        values = {m: page.model.metrics.get(m) for m in metric_list}
        label.text = format_fn(values)
        if color_fn:
            state = color_fn(values)
            if state is not None and state != last_state[0]:
                last_state[0] = state
                label.style(f'color: {STATUS_COLORS[state]}')
    return _update


def _make_color_update(page, label, metric_name: str, color_rule):
    """Build a refresh callback recoloring a single-metric card, restyling only when the rule's state changes."""
    from vigil.core.ui.theme import STATUS_COLORS
    last_state = [None]

    def _update():
        state = color_rule(page.model.metrics.get(metric_name))
        if state is not None and state != last_state[0]:
            last_state[0] = state
            label.style(f'color: {STATUS_COLORS[state]}')
    return _update


def _page_metric_names(cards: Dict[str, Any], charts: Dict[str, Any], layout) -> List[str]:
    """Collect the metric names the rendered cards and charts subscribe the page to."""
    metric_names = [c['metric'] for name, c in cards.items()
                    if 'metric' in c and name != 'status_card' and layout.renders(name)]
    for name, c in cards.items():
        if layout.renders(name):
            metric_names += c.get('metrics', [])
    metric_names += [c['metric'] for name, c in charts.items() if layout.renders(name)]
    return metric_names


def _render_value_card(plugin: Any, layout, widget_name: str, title: str, card_spec):
    """Render a static or plugin-attribute card and return its refresh update, if it declares one."""
    from vigil.core.ui.components import info_card
    if 'value_attr' in card_spec:
        value = getattr(plugin, card_spec['value_attr'])
        text = card_spec.get('value_format', '{}').format(value)
    else:
        text = card_spec.get('value', '--')
    with layout.cell(widget_name):
        label = info_card(title, text)
    color_attr = card_spec.get('color_attr')
    if color_attr or card_spec.get('refresh'):
        return _make_attr_update(plugin, label, card_spec.get('value_attr'),
                                 card_spec.get('value_format', '{}'), color_attr)
    return None


def _render_multi_metric_card(page, layout, widget_name: str, title: str, card_spec):
    """Render a card combining several metrics through an item formatter and return its refresh update."""
    from vigil.core.ui.components import info_card
    format_fn = resolve(ITEM_FORMATTERS, card_spec['format_fn'], required=True)
    color_fn = resolve(ITEM_COLOR_RULES, card_spec.get('color_fn'))
    with layout.cell(widget_name):
        label = info_card(title, format_fn({}))
    return _make_multi_update(page, label, card_spec['metrics'], format_fn, color_fn)


def _render_metric_card(page, layout, widget_name: str, title: str, card_spec):
    """Render a card bound live to one formatted metric and return its color update, if a color rule is set."""
    from vigil.core.ui.components import info_card
    metric_name = card_spec['metric']
    fmt_name = card_spec.get('format', 'int')
    formatter = resolve(FORMATTERS, fmt_name)
    if formatter is None:
        raise KeyError(
            f"UI_SPEC card {widget_name!r} references unknown format {fmt_name!r} "
            f"— register it via spec.register_formatter first"
        )
    with layout.cell(widget_name):
        label = info_card(title, formatter(None)).bind_text_from(
            page.model, ('metrics', metric_name), backward=formatter)
    color_name = card_spec.get('color')
    if not color_name:
        return None
    color_rule = resolve(COLOR_RULES, color_name)
    if color_rule is None:
        raise KeyError(
            f"UI_SPEC card {widget_name!r} references unknown color rule {color_name!r} "
            f"— register it via spec.register_color_rule first"
        )
    return _make_color_update(page, label, metric_name, color_rule)


def _render_cards(plugin: Any, page, layout, cards: Dict[str, Any]) -> list:
    """Render every plain card by its first-matching kind and collect their refresh updates."""
    from vigil.core.ui.components import render_repeat_card
    updates = []
    for widget_name, card_spec in cards.items():
        if widget_name == 'host_card' or widget_name == 'status_card':
            continue
        if not layout.renders(widget_name):
            continue
        if 'repeat' in card_spec:
            with layout.cell(widget_name):
                render_repeat_card(plugin, page, card_spec['repeat'])
            continue
        title = card_spec['title']
        if 'metrics' in card_spec:
            update = _render_multi_metric_card(page, layout, widget_name, title, card_spec)
        elif 'metric' in card_spec:
            update = _render_metric_card(page, layout, widget_name, title, card_spec)
        else:
            update = _render_value_card(plugin, layout, widget_name, title, card_spec)
        if update is not None:
            updates.append(update)
    return updates


def _render_host_card(plugin: Any, layout):
    """Render the shared host summary card when the layout places it."""
    if layout.hosts('host_card') and layout.renders('host_card'):
        with layout.cell('host_card'):
            plugin.ui.host_card()


def _render_status_card(plugin: Any, page, layout, cards: Dict[str, Any]):
    """Render the reserved on/off status card when the spec declares one."""
    if 'status_card' not in cards or not layout.renders('status_card'):
        return
    sc = cards['status_card']
    with layout.cell('status_card'):
        plugin.ui.status_card(
            page,
            metric_name=sc['metric'],
            title=sc.get('title', 'STATUS'),
            on_text=sc.get('on_text', 'ACTIVE'),
            off_text=sc.get('off_text', 'INACTIVE'),
        )


def _render_charts(plugin: Any, page, layout, charts: Dict[str, Any]):
    """Render one history chart per declared chart widget."""
    from vigil.core.ui.components import history_chart
    for widget_name, cs in charts.items():
        if not layout.renders(widget_name):
            continue
        with layout.cell(widget_name):
            history_chart(page, cs.get('title', cs['metric'].upper()),
                          plugin.id, cs['metric'])


def _render_events(plugin: Any, page, layout, show_events):
    """Render the plugin events table when the spec enables it."""
    if not show_events or not layout.renders('events'):
        return
    events_kwargs = show_events if isinstance(show_events, dict) else {}
    with layout.cell('events'):
        plugin.ui.events_table(page, **events_kwargs)


def _render_tables(plugin: Any, page, layout, tables: Dict[str, Any], filters: Dict[str, Any]):
    """Render each declared table with its row actions and optional filter."""
    from vigil.core.ui.components import render_table_with_actions
    for widget_name, table_spec in tables.items():
        if not layout.renders(widget_name):
            continue
        with layout.cell(widget_name):
            render_table_with_actions(plugin, page, table_spec, filters.get(widget_name))


def _render_buttons(plugin: Any, layout, buttons: Dict[str, Any]):
    """Render each declared row of action buttons."""
    from vigil.core.ui.components import render_buttons
    for widget_name, button_specs in buttons.items():
        if not layout.renders(widget_name):
            continue
        with layout.cell(widget_name):
            render_buttons(plugin, button_specs)


def _render_job_panel(plugin: Any, layout, job_panel_spec):
    """Render the run/cancel job panel when the spec declares one."""
    from vigil.core.ui.components import render_job_panel
    if not job_panel_spec:
        return
    widget = job_panel_spec.get('widget', 'jobs')
    if not layout.renders(widget):
        return
    with layout.cell(widget):
        render_job_panel(plugin, job_panel_spec)


def generic_render(plugin: Any, context: str = 'page', spec: Optional[UISpec] = None,
                   page=None, start: bool = True, layout=None):
    """Build a plugin's UI from its UI_SPEC by dispatching each top-level key to its renderer."""
    spec = spec if spec is not None else getattr(plugin, 'UI_SPEC', None)
    if spec is None:
        raise ValueError(
            f"{plugin.__class__.__name__} has no UI_SPEC and none was passed to generic_render()"
        )

    cards = spec.get('cards', {})
    charts = dict(spec.get('charts', {}))
    chart_spec = spec.get('chart')
    if chart_spec:
        charts.setdefault('chart', chart_spec)

    if layout is None:
        from vigil.core.ui.layout import PluginLayout, make_inline_layout
        layout_rows = spec.get('layout', [])
        layout = PluginLayout(
            plugin.config,
            layout_rows if context == 'page' else make_inline_layout(layout_rows),
        )

    if page is None:
        page = plugin.ui.page(metric_names=_page_metric_names(cards, charts, layout))

    color_updates = _render_cards(plugin, page, layout, cards)
    _render_host_card(plugin, layout)
    _render_status_card(plugin, page, layout, cards)
    _render_charts(plugin, page, layout, charts)
    _render_events(plugin, page, layout, spec.get('events', False))
    _render_tables(plugin, page, layout, spec.get('tables', {}), spec.get('filters', {}))
    _render_buttons(plugin, layout, spec.get('buttons', {}))
    _render_job_panel(plugin, layout, spec.get('job_panel'))

    if color_updates:
        def _update_all_colors():
            for update in color_updates:
                update()
        page.on_refresh(_update_all_colors)

    if start:
        page.start()

    return page
