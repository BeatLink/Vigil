"""TypedDict shapes for a plugin's UI_SPEC property, interpreted by
spec.generic_render(). See DEVELOP.md's "Declarative UI spec" section for
why this exists as data rather than hand-written render_ui() calls.

Every field is optional at the TypedDict level (total=False) because
generic_render() itself treats a missing key as "this widget isn't used" —
these types document the *shape* a present key must have, not which keys
a given plugin needs. KeyError/ValueError at render time (e.g. an unknown
format name) remains the actual validation; nothing here is enforced at
class-definition time, since UI_SPEC is a plain @property, not a dataclass.
"""

from typing import Any, Callable, Dict, List, Optional, TypedDict, Union


class CardSpec(TypedDict, total=False):
    """One info_card. Exactly one of `metric`, `metrics`, `value_attr`,
    `value`, or `repeat` should be set — generic_render checks them in
    that priority order and the first match wins."""
    title: str
    metric: str                    # bind_text_from a single metric, live-updating
    format: str                    # FORMATTERS key: Optional[float] -> str
    color: str                     # COLOR_RULES key: Optional[float] -> Optional[str state]
    metrics: List[str]             # combine several metrics into one card
    format_fn: str                 # ITEM_FORMATTERS key: dict[metric,value] -> str
    color_fn: str                  # ITEM_FORMATTERS key: dict[metric,value] -> Optional[str state]
    value_attr: str                # getattr(plugin, value_attr) each refresh
    value_format: str              # '{}'.format() template applied to value_attr
    color_attr: str                # getattr(plugin, color_attr) -> a STATUS_COLORS key
    refresh: bool                  # re-read value_attr/color_attr every page tick
    value: str                     # static text, computed once at page load
    repeat: "RepeatSpec"           # render_repeat_card instead of a single value
    # status_card only:
    on_text: str
    off_text: str


class RepeatSpec(TypedDict, total=False):
    """One 'family' of dynamically-discovered items (disks, filesystems,
    GPUs, ...) rendered as repeated chips or mini-cards. `source` selects
    which of the three resolution strategies _resolve_repeat_items uses."""
    source: str                    # 'snapshot' (default) | 'setting' | 'metrics_prefix'
    container: str                 # 'chips' (default) | anything else -> info_card grid
    item_label: str
    item_value: str
    item_format: str               # FORMATTERS key
    item_format_fn: str            # ITEM_FORMATTERS key, takes precedence over item_format
    label_transform: str           # 'none' (default) | 'slashes' | 'spaces_upper'
    item_label_prefix: str
    item_label_suffix: str
    item_color_by: str             # ITEM_COLOR_RULES key
    empty_text: str
    # source == 'setting':
    setting_key: str               # '{plugin_id}'-formatted
    dict_fields: List[str]
    # source == 'metrics_prefix':
    metrics_prefix: str
    metrics_suffix: str
    metrics_exclude: List[str]
    metrics_scan_limit: int
    fields: List["MetricFieldSpec"]


class MetricFieldSpec(TypedDict):
    name: str
    prefix: str
    suffix: str


class ChartSpec(TypedDict, total=False):
    title: str
    metric: str


class DynamicChartsSpec(TypedDict, total=False):
    widget: str                    # cell name, defaults to 'charts'
    items_attr: str                # getattr(plugin, items_attr) -> Iterable[(title, metric_name)]


class ColumnSpec(TypedDict, total=False):
    name: str
    label: str
    field: str
    align: str
    sortable: bool
    cell_color_by: str             # ITEM_COLOR_RULES key, applied per-row


class RowActionSpec(TypedDict, total=False):
    id: str
    icon: str
    color: str
    tooltip: str
    visible_if: str                # ENABLED_PREDICATES key
    kind: str                      # 'dialog' routes to `dialog`; anything else dispatches an action
    dialog: str                    # dialog name (see DialogSpec), when kind == 'dialog'
    action_id: str                 # defaults to `id` when omitted
    params: Dict[str, str]         # kwarg name -> row field name
    static_params: Dict[str, Any]
    notify: bool                   # default True


class TableSpec(TypedDict, total=False):
    row_key: str                   # default 'id'
    columns: List[ColumnSpec]
    row_actions: List[RowActionSpec]
    rows_attr: str                 # getattr(plugin, rows_attr) instead of a repeat-item source
    source: str                    # _resolve_repeat_items source, when rows_attr is absent


class FilterSpec(TypedDict, total=False):
    placeholder: str
    fields: List[str]              # row keys searched against the filter text


class ButtonSpec(TypedDict, total=False):
    id: str
    label: str
    icon: str
    color: str
    flat: bool                     # default True
    visible_if: str                # ENABLED_PREDICATES key
    kind: str                      # 'dialog' routes to `dialog`; anything else dispatches on_action(id)
    dialog: str
    notify: bool                   # default True


class DialogSpec(TypedDict, total=False):
    """One entry under UI_SPEC['dialogs'], resolved by _dialog_spec_for()
    and rendered by components.open_dialog_impl()."""
    kind: str                      # 'read' | 'edit'
    title: str                     # '{row[...]}' / '{plugin.attr}' template, see components._substitute
    render: str                    # 'read' only: 'textarea_readonly' or plain label
    # kind == 'read':
    action_id: str
    params: Dict[str, str]
    # kind == 'edit':
    load_action_id: str
    load_params: Dict[str, str]
    save_action_id: str
    save_params: Dict[str, str]
    save_content_kwarg: str        # default 'content'
    success_message: str


class JobPanelSpec(TypedDict, total=False):
    widget: str                    # cell name, defaults to 'jobs'
    title: str
    run_label: str
    run_icon: str
    cancel_label: str
    cancel_icon: str
    enabled_if: str                # ENABLED_PREDICATES key
    run_action_id: str
    history_limit: int


# UI_SPEC['layout'] rows: each row is a list of either a bare widget-name
# string, or {'widget': name, **per-widget cell overrides}. layout.py's
# resolve_rows() is what reads them (visible, height, flex, min_width, title),
# and a group's own layout uses the same shape with '<child_id>.<widget>' names.
LayoutRow = List[Union[str, Dict[str, Any]]]


class UISpec(TypedDict, total=False):
    layout: List[LayoutRow]
    cards: Dict[str, CardSpec]
    chart: ChartSpec                       # shorthand for charts={'chart': ...}
    charts: Dict[str, ChartSpec]
    dynamic_charts: DynamicChartsSpec
    events: Union[bool, Dict[str, Any]]    # True, or kwargs forwarded to UIOrchestrator.events_table
    logs: Union[bool, Dict[str, Any]]      # True, or kwargs forwarded to UIOrchestrator.logs_table
    tables: Dict[str, TableSpec]
    filters: Dict[str, FilterSpec]         # keyed by the same widget_name as `tables`
    buttons: Dict[str, List[ButtonSpec]]
    dialogs: Dict[str, DialogSpec]
    job_panel: JobPanelSpec


# Registry callable shapes — formalizes what register_formatter/
# register_color_rule/register_item_formatter/register_item_color_rule/
# register_enabled_predicate actually require, previously expressed only
# as inline Callable[...] annotations on each Dict[str, Callable] registry.
Formatter = Callable[[Optional[float]], str]
ColorRule = Callable[[Optional[float]], Optional[str]]
ItemFormatter = Callable[[dict], str]
ItemColorRule = Callable[[dict], Optional[str]]
EnabledPredicate = Callable[[Any], bool]
