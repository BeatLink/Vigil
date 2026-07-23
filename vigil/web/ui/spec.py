"""
Declarative UI spec for plugin dashboard pages.

A plugin can declare `UI_SPEC` on its `*UIPlugin` class instead of hand-writing
render_ui(): a dict describing the layout grid, metric cards, chart, and
whether to show the events/logs tables. `generic_render(plugin, context)`
interprets it and builds the page — the same PluginPage/PluginModel binding
machinery (see model.py) underneath, just generated instead of hand-written.

This targets the common case a majority of plugins already reduced to after
the binding-model migration: a handful of `info_card(...).bind_text_from(...)`
calls with a small formatter, a layout grid, a chart, an events table. Plugins
with genuinely bespoke widgets (processes.py's per-row kill buttons, borg.py's
job panel, service_list.py's unit-file editor) keep a real render_ui() —
`generic_render()` is a plain function they can still call for the standard
parts of their page, not a hook every plugin must fit into.

Format/color functions are referenced BY NAME from FORMATTERS/COLOR_RULES
(below) rather than declared as inline lambdas, so the spec dict stays pure
data — serializable, diffable, and reusable across plugins without each one
redefining "show one decimal place" from scratch. A plugin needing a
genuinely one-off transform registers it under its own key (register_formatter/
register_color_rule) rather than bending a shared name to fit.

Example (frigate.py, condensed):

    UI_SPEC = {
        'layout': [
            ['host_card', 'quality_card', 'fps_card'],
            ['detector_card', 'stalls_card', 'reconnects_card'],
            ['chart'],
            ['events'],
        ],
        'cards': {
            'quality_card': {'metric': 'worst_quality_rank', 'title': 'WORST QUALITY',
                             'format': 'quality_rank', 'color': 'quality_rank_color'},
            'fps_card': {'metric': 'camera_fps_total', 'title': 'CAMERA FPS', 'format': 'decimal1'},
            'detector_card': {'metric': 'detector_inference_ms', 'title': 'INFERENCE', 'format': 'ms1'},
            'stalls_card': {'metric': 'stalls_last_hour', 'title': 'STALLS/H',
                            'format': 'int', 'color': 'nonzero_warning'},
            'reconnects_card': {'metric': 'reconnects_last_hour', 'title': 'RECONNECTS/H',
                                'format': 'int', 'color': 'nonzero_warning'},
        },
        'chart': {'metric': 'camera_fps_total', 'title': 'CAMERA FPS'},
        'events': True,
    }
"""
from typing import Any, Callable, Dict, Optional

# ---------------------------------------------------------------------------
# Formatter registry
#
# Each formatter takes the raw metric value (float) or None (metric not yet
# collected) and returns display text. Named variants are kept DISTINCT where
# existing plugins' hand-written formatters actually differed (rounding
# precision, dash text) — collapsing e.g. both `.0f` and `.1f` percent
# formatters into one 'percent' entry would silently change some plugins'
# rendered output, verified by auditing the real formatters in use before
# this registry was written (12 plugins' `_pct_or_dash`, split between .0f
# and .1f precision — both are kept, as 'percent0'/'percent1').
# ---------------------------------------------------------------------------
FORMATTERS: Dict[str, Callable[[Optional[float]], str]] = {}


def register_formatter(name: str):
    """Decorator: `@register_formatter('my_thing')` adds a named formatter,
    for plugins whose card needs a transform not already in FORMATTERS."""
    def wrap(fn):
        FORMATTERS[name] = fn
        return fn
    return wrap


def _fmt(value):
    return '--' if value is None else value


@register_formatter('int')
def _int(v):
    return '--' if v is None else str(int(v))

@register_formatter('count_comma')
def _count_comma(v):
    """Thousands-separated integer, e.g. '12,345' — for counters (queries,
    interrupts) where a bare int is hard to read at a glance."""
    return '--' if v is None else f'{int(v):,}'

@register_formatter('decimal1')
def _decimal1(v):
    return '--' if v is None else f'{v:.1f}'

@register_formatter('percent0')
def _percent0(v):
    return '-- %' if v is None else f'{v:.0f}%'

@register_formatter('percent1')
def _percent1(v):
    return '-- %' if v is None else f'{v:.1f}%'

@register_formatter('ms1')
def _ms1(v):
    return '--' if v is None else f'{v:.1f} ms'

@register_formatter('seconds_ms')
def _latency_ms(v):
    return '-- ms' if v is None else f'{v:.1f} ms'

@register_formatter('bytes_gb')
def _bytes_gb(v):
    """GB value through plugin_utils.format_bytes (auto MB/GB/TB scaling)."""
    from vigil.core.common.plugin_utils import format_bytes
    return '--' if v is None else format_bytes(v)

@register_formatter('kbps_rate')
def _kbps_rate(v):
    """KB/s value, auto-scaled to MB/s past 1024 — matches diskio.py/
    network_usage.py's existing _format_rate."""
    if v is None:
        return '-- KB/s'
    if v >= 1024:
        return f'{v / 1024:.1f} MB/s'
    return f'{v:.1f} KB/s'

@register_formatter('on_off')
def _on_off(v):
    if v is None:
        return 'Checking...'
    return 'ON' if v > 0.5 else 'OFF'


# ---------------------------------------------------------------------------
# Color-rule registry
#
# Each rule takes the raw metric value and returns a STATUS_COLORS key
# ('online'/'warning'/'failed') or None (leave the card's default color).
# ---------------------------------------------------------------------------
COLOR_RULES: Dict[str, Callable[[Optional[float]], Optional[str]]] = {}


def register_color_rule(name: str):
    def wrap(fn):
        COLOR_RULES[name] = fn
        return fn
    return wrap


@register_color_rule('nonzero_warning')
def _nonzero_warning(v):
    """Warning color once the metric is above zero — for counters that
    should read as fine at 0 and worth noticing otherwise (stalls,
    reconnects, OOM kills)."""
    if v is None:
        return None
    return 'warning' if v > 0 else 'online'




def threshold_color(warning: float, threshold: float):
    """Factory for a color rule mirroring plugin_utils.level_for — for
    plugins whose threshold is config-driven rather than a fixed constant,
    register the result under a plugin-specific name:
    `register_color_rule('my_threshold')(threshold_color(cfg_warn, cfg_fail))`.
    """
    def rule(v):
        if v is None:
            return None
        from vigil.core.common.plugin_utils import level_for
        level = level_for(v, warning, threshold)
        return {'online': 'online', 'warning': 'warning', 'failed': 'failed'}[level]
    return rule


# ---------------------------------------------------------------------------
# Generic renderer
# ---------------------------------------------------------------------------

def generic_render(plugin: Any, context: str = 'page', spec: Optional[Dict[str, Any]] = None,
                   page=None, start: bool = True):
    """
    Build a plugin's dashboard page from its UI_SPEC (or an explicitly passed
    `spec`, for plugins that call this for only PART of their page — see
    processes.py-style plugins, which render standard cards via this function
    and a bespoke table by hand around it).

    `page`: pass an existing PluginPage (e.g. one already carrying extra
    tracked metric names for a caller's own custom widgets) to render into
    it instead of constructing a fresh one. `start=False` skips calling
    page.start() — use when the caller has more widgets to add first and
    will call page.start() itself once everything is built.

    Returns the PluginPage used, so callers extending the page can keep
    using it (`page.on_refresh(...)`, `page.track_metric(...)`, etc.).
    """
    from nicegui import ui
    from vigil.web.ui.layout import PluginLayout, make_inline_layout
    from vigil.web.ui.components import info_card, history_chart
    from vigil.web.ui.theme import STATUS_COLORS

    spec = spec if spec is not None else getattr(plugin, 'UI_SPEC', None)
    if spec is None:
        raise ValueError(
            f"{plugin.__class__.__name__} has no UI_SPEC and none was passed to generic_render()"
        )

    layout_rows = spec.get('layout', [])
    cards = spec.get('cards', {})
    chart_spec = spec.get('chart')
    show_events = spec.get('events', False)
    show_logs = spec.get('logs', False)

    layout = PluginLayout(
        plugin.config,
        layout_rows if context == 'page' else make_inline_layout(layout_rows),
    )

    if page is None:
        metric_names = [c['metric'] for c in cards.values() if 'metric' in c]
        if chart_spec:
            metric_names.append(chart_spec['metric'])
        page = plugin.page(metric_names=metric_names)

    color_updates = []

    for widget_name, card_spec in cards.items():
        if widget_name == 'host_card' or widget_name == 'status_card':
            continue  # handled below as special-cased standard widgets
        metric_name = card_spec['metric']
        title = card_spec['title']
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
            plugin.internal_modules['ui']['host_card']()

    if 'status_card' in cards:
        sc = cards['status_card']
        with layout.cell('status_card'):
            plugin.internal_modules['ui']['status_card'](
                page,
                metric_name=sc['metric'],
                title=sc.get('title', 'STATUS'),
                on_text=sc.get('on_text', 'ACTIVE'),
                off_text=sc.get('off_text', 'INACTIVE'),
            )

    if chart_spec:
        with layout.cell('chart'):
            history_chart(page, chart_spec.get('title', chart_spec['metric'].upper()),
                          plugin.id, chart_spec['metric'])

    if show_events:
        with layout.cell('events'):
            plugin.internal_modules['ui']['events_table'](page)

    if show_logs:
        logs_kwargs = show_logs if isinstance(show_logs, dict) else {}
        with layout.cell('logs'):
            plugin.internal_modules['ui']['logs_table'](page, **logs_kwargs)

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
