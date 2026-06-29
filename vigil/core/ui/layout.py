from contextlib import contextmanager
from typing import Dict, Any, Tuple


def make_inline_layout(
    default_layout: dict,
    hidden: Tuple[str, ...] = ('host_card', 'logs'),
) -> dict:
    """Return a copy of *default_layout* with *hidden* widgets set to visible=False.

    Used to produce the compact variant rendered inside group expansion panels,
    where the host header and log table are redundant context.
    """
    widgets = {
        name: ({**defn, 'visible': False} if name in hidden else {**defn})
        for name, defn in default_layout.get('widgets', {}).items()
    }
    return {**default_layout, 'widgets': widgets}


class PluginLayout:
    """
    CSS grid layout manager for plugin UIs.

    Each plugin defines a ``_DEFAULT_LAYOUT`` with ``grid_columns`` and a
    ``widgets`` dict that maps widget names to their default placement::

        _DEFAULT_LAYOUT = {
            'grid_columns': 2,
            'widgets': {
                'host_card': {'col_span': 1},
                'value_card': {'col_span': 1},
                'chart':     {'col_span': 2},
                'logs':      {'col_span': 2},
            }
        }

    Users can override any of these from ``config.yaml`` under a ``layout:``
    key on the plugin entry::

        layout:
          grid_columns: 3
          chart:
            col_span: 3
            row_span: 2
            height: "400px"
          logs:
            visible: false

    Per-widget options:
      col        Start column (1-based). Omit to use CSS auto-placement.
      row        Start row    (1-based). Omit to use CSS auto-placement.
      col_span   How many columns this widget occupies (default: 1).
      row_span   How many rows   this widget occupies (default: 1).
      height     Explicit CSS height string, e.g. ``"400px"`` (default: auto).
      visible    Whether to render this widget (default: true). When false the
                 cell is created with ``display: none`` so reactive timers that
                 reference it still function safely.
    """

    def __init__(self, plugin_config: dict, default_layout: dict) -> None:
        from nicegui import ui

        user = plugin_config.get('layout', {})

        self.grid_columns: int = int(
            user.get('grid_columns') or
            default_layout.get('grid_columns', 1)
        )

        defaults: Dict[str, dict] = default_layout.get('widgets', {})
        self._widgets: Dict[str, dict] = {
            name: {**defn, **(user.get(name) or {})}
            for name, defn in defaults.items()
        }
        for name, defn in user.items():
            if name not in self._widgets and name != 'grid_columns':
                self._widgets[name] = defn

        self._container = ui.element('div').style(
            f'display: grid; '
            f'grid-template-columns: repeat({self.grid_columns}, 1fr); '
            f'gap: 1rem; width: 100%;'
        )

    @contextmanager
    def cell(self, widget_name: str):
        """Context manager that places its contents in a named grid cell."""
        from nicegui import ui

        cfg = self._widgets.get(widget_name, {})

        col      = cfg.get('col')
        row      = cfg.get('row')
        col_span = int(cfg.get('col_span', 1))
        row_span = int(cfg.get('row_span', 1))
        height   = cfg.get('height')
        visible  = cfg.get('visible', True)

        parts: list[str] = []
        if col is not None:
            parts.append(f'grid-column: {col} / span {col_span}')
        else:
            parts.append(f'grid-column: span {col_span}')
        if row is not None:
            parts.append(f'grid-row: {row} / span {row_span}')
        else:
            parts.append(f'grid-row: span {row_span}')
        if height:
            parts.append(f'height: {height}; overflow-y: auto')
        if not visible:
            parts.append('display: none')

        with self._container:
            div = ui.element('div').style('; '.join(parts))
        with div:
            yield div
