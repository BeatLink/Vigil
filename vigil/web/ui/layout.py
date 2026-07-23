from contextlib import contextmanager
from typing import Any, Dict, List, Tuple, Union


def make_inline_layout(
    default_layout: list,
    hidden: Tuple[str, ...] = ('host_card', 'logs'),
) -> list:
    """Return a copy of *default_layout* with *hidden* widgets set to visible=False.

    Used to produce the compact variant rendered inside group expansion panels,
    where the host header and log table are redundant context.
    """
    result = []
    for row in default_layout:
        new_row = []
        for item in row:
            if isinstance(item, str):
                name, base = item, {}
            else:
                name = item['widget']
                base = {k: v for k, v in item.items() if k != 'widget'}
            if name in hidden:
                new_row.append({'widget': name, **base, 'visible': False})
            else:
                new_row.append(item)
        result.append(new_row)
    return result


class PluginLayout:
    """
    Flex-row layout manager for plugin UIs.

    Each plugin defines a ``_DEFAULT_LAYOUT`` as a list of rows. Each row is a
    list of widget names. Widgets in the same row are placed side by side with
    equal width; a widget alone in a row fills the full width::

        _DEFAULT_LAYOUT = [
            ['host_card', 'cpu_card'],  # two stat cards side by side
            ['chart'],                  # full width
            ['logs'],                   # full width
        ]

    Users can override from ``config.yaml`` under a ``layout:`` key in two ways:

    Full row-structure override (replaces defaults entirely):
        layout:
          - [host_card, cpu_card, chart]
          - [logs]

    Per-widget property overrides (keeps default row structure, tweaks specific widgets):
        layout:
          logs:
            visible: false
          chart:
            height: "400px"
            flex: 2

    Per-widget options:
      flex     Relative width within the row (CSS flex value, default: 1).
      height   Explicit CSS height string, e.g. ``"400px"`` (default: auto).
      visible  Whether to show this widget (default: true). When false the
               cell is hidden so reactive timers referencing it remain safe.
    """

    def __init__(self, plugin_config: dict, default_layout: list) -> None:
        from nicegui import ui

        user = plugin_config.get('layout', {})

        if isinstance(user, list):
            raw_rows = user
            widget_overrides: Dict[str, dict] = {}
        else:
            raw_rows = default_layout
            widget_overrides = user

        self._widget_row_div: Dict[str, Any] = {}
        self._widget_cfg: Dict[str, dict] = {}

        outer = ui.element('div').style(
            'display: flex; flex-direction: column; gap: 1rem; width: 100%'
        )

        for row in raw_rows:
            items: List[Tuple[str, dict]] = []
            for item in row:
                if isinstance(item, str):
                    name, base_cfg = item, {}
                else:
                    name = item['widget']
                    base_cfg = {k: v for k, v in item.items() if k != 'widget'}
                override = widget_overrides.get(name, {})
                cfg = {
                    'flex':    int(base_cfg.get('flex',    override.get('flex',    1))),
                    'visible': bool(base_cfg.get('visible', override.get('visible', True))),
                    'height':  base_cfg.get('height') or override.get('height'),
                }
                self._widget_cfg[name] = cfg
                items.append((name, cfg))

            all_hidden = all(not cfg['visible'] for _, cfg in items)
            row_style = (
                'display: none'
                if all_hidden else
                'display: flex; gap: 1rem; width: 100%; align-items: stretch'
            )
            with outer:
                row_div = ui.element('div').style(row_style)
            for name, _ in items:
                self._widget_row_div[name] = row_div

    @contextmanager
    def cell(self, widget_name: str):
        """Context manager that places its contents in the named widget's row slot."""
        from nicegui import ui

        cfg     = self._widget_cfg.get(widget_name, {})
        flex    = int(cfg.get('flex', 1))
        visible = bool(cfg.get('visible', True))
        height  = cfg.get('height')

        parts = [f'flex: {flex}; min-width: 0']
        if height:
            parts.append(f'height: {height}; overflow-y: auto')
        if not visible:
            parts.append('display: none')

        row_div = self._widget_row_div.get(widget_name)
        if row_div is None:
            # Widget not in layout — still create the element so timers stay safe
            parent = ui.element('div').style('display: none')
        else:
            parent = row_div

        with parent:
            div = ui.element('div').style('; '.join(parts))
        with div:
            yield div
