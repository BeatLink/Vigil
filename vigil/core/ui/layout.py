from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vigil.core.settings.config_schema import PluginConfig
from vigil.core.ui.spec_types import LayoutRow

ResolvedRow = List[Tuple[str, dict]]


def split_item(item: Any) -> Tuple[str, dict]:
    """Normalize one layout entry into its widget name and its property overrides."""
    if isinstance(item, str):
        return item, {}
    return item['widget'], {k: v for k, v in item.items() if k != 'widget'}


def resolve_rows(raw_rows: List[LayoutRow],
                 widget_overrides: Dict[str, dict]) -> List[ResolvedRow]:
    """Pure: merge per-widget config overrides onto declared rows, filling defaults."""
    resolved: List[ResolvedRow] = []
    for row in raw_rows:
        items: ResolvedRow = []
        for item in row:
            name, base = split_item(item)
            override = widget_overrides.get(name, {})
            items.append((name, {
                'flex':      int(base.get('flex',    override.get('flex', 1))),
                'visible':   bool(base.get('visible', override.get('visible', True))),
                'height':    base.get('height')    or override.get('height'),
                'min_width': base.get('min_width') or override.get('min_width') or '280px',
                'title':     base.get('title')     or override.get('title'),
            }))
        resolved.append(items)
    return resolved


def make_inline_layout(
    default_layout: List[LayoutRow],
    hidden: Tuple[str, ...] = ('host_card', 'logs'),
) -> List[LayoutRow]:
    result = []
    for row in default_layout:
        new_row = []
        for item in row:
            name, base = split_item(item)
            if name in hidden:
                new_row.append({'widget': name, **base, 'visible': False})
            else:
                new_row.append(item)
        result.append(new_row)
    return result


def _row_style(all_hidden: bool) -> str:
    if all_hidden:
        return 'display: none'
    return 'display: flex; flex-wrap: wrap; gap: 1rem; width: 100%; align-items: stretch'


def _cell_style(cfg: dict) -> str:
    parts = [f"flex: {cfg['flex']} 1 {cfg['min_width']}; min-width: 0"]
    if cfg['height']:
        parts.append(f"height: {cfg['height']}; overflow-y: auto")
    if not cfg['visible']:
        parts.append('display: none')
    return '; '.join(parts)


class PluginLayout:
    """`plugin_config['layout']`, if set, is polymorphic: either a full
    List[LayoutRow] replacing default_layout's row structure entirely, or a
    Dict[str, dict] of per-widget property overrides (visible/height/flex/
    min_width) merged onto default_layout's existing rows. Distinguished
    at runtime by isinstance since UI_SPEC config is plain YAML-sourced
    data with no tag to dispatch on structurally.

    Every cell div is created up front, in declared order, so the page follows
    the layout rather than the order generic_render happens to build widgets in."""

    def __init__(self, plugin_config: PluginConfig, default_layout: List[LayoutRow]) -> None:
        from nicegui import ui

        user = plugin_config.get('layout', {})
        if isinstance(user, list):
            raw_rows, widget_overrides = user, {}
        else:
            raw_rows, widget_overrides = default_layout, user

        self._widget_cfg: Dict[str, dict] = {}
        self._widget_div: Dict[str, Any] = {}

        outer = ui.element('div').style(
            'display: flex; flex-direction: column; gap: 1rem; width: 100%'
        )

        for row in resolve_rows(raw_rows, widget_overrides):
            all_hidden = all(not cfg['visible'] for _, cfg in row)
            with outer:
                row_div = ui.element('div').style(_row_style(all_hidden))
            for name, cfg in row:
                self._widget_cfg[name] = cfg
                with row_div:
                    self._widget_div[name] = ui.element('div').style(_cell_style(cfg))

    def widgets(self) -> Iterable[str]:
        """Every widget name this layout has a cell for."""
        return self._widget_cfg.keys()

    def config_for(self, widget_name: str) -> dict:
        """The resolved cell properties of one widget, empty if it has no cell."""
        return self._widget_cfg.get(widget_name, {})

    def hosts(self, widget_name: str) -> bool:
        """Whether the layout places this widget at all."""
        return widget_name in self._widget_cfg

    def renders(self, widget_name: str) -> bool:
        """Whether generic_render should build this widget; a plain layout builds all of them."""
        return True

    @contextmanager
    def cell(self, widget_name: str):
        from nicegui import ui

        div = self._widget_div.get(widget_name)
        if div is None:
            div = ui.element('div').style('display: none')
        with div:
            yield div


class CompositeLayout:
    """A group's own grid, whose cells address a descendant's widget as
    '<child_id>.<widget_name>' so elements of different children can share a
    row. `view()` hands one child a PluginLayout-shaped window onto that grid:
    the child's generic_render() sees its own widget names and builds only the
    ones the group placed, into the group's cells."""

    def __init__(self, rows: List[LayoutRow]) -> None:
        self._grid = PluginLayout({}, rows)
        self._claimed: set = set()

    @staticmethod
    def _parse(ref: str) -> Tuple[str, Optional[str]]:
        """Split a 'child_id.widget' ref; a ref with no dot names a whole child."""
        child_id, sep, widget = ref.rpartition('.')
        return (child_id, widget) if sep else (ref, None)

    def widgets_for(self, child_id: str) -> Dict[str, dict]:
        """The widget names this grid places for one child, mapped to their cell config."""
        return {
            widget: self._grid.config_for(ref)
            for ref in self._grid.widgets()
            for owner, widget in [self._parse(ref)]
            if owner == child_id and widget is not None
        }

    def hosts_child(self, child_id: str) -> bool:
        """Whether the grid places the child whole, by a bare id with no widget suffix."""
        return self._grid.hosts(child_id)

    def config_for(self, ref: str) -> dict:
        """The resolved cell properties of one ref, empty if the grid has no such cell."""
        return self._grid.config_for(ref)

    def unclaimed(self) -> List[str]:
        """Visible refs no descendant ever rendered into — a misspelled id or widget name."""
        return sorted(ref for ref in self._grid.widgets()
                      if ref not in self._claimed and self._grid.config_for(ref)['visible'])

    @contextmanager
    def cell(self, ref: str):
        self._claimed.add(ref)
        with self._grid.cell(ref) as div:
            yield div

    def view(self, child_id: str) -> "ChildLayoutView":
        return ChildLayoutView(self, child_id)


class ChildLayoutView:
    """One child's window onto a CompositeLayout: it answers in the child's own
    widget names and forwards to the group's '<child_id>.<widget>' cells."""

    def __init__(self, composite: CompositeLayout, child_id: str) -> None:
        self._composite = composite
        self._child_id = child_id
        self._grid = composite._grid

    def _ref(self, widget_name: str) -> str:
        return f'{self._child_id}.{widget_name}'

    def hosts(self, widget_name: str) -> bool:
        return self._grid.hosts(self._ref(widget_name))

    def renders(self, widget_name: str) -> bool:
        cfg = self._grid.config_for(self._ref(widget_name))
        return bool(cfg) and cfg['visible']

    def cell(self, widget_name: str):
        return self._composite.cell(self._ref(widget_name))
