"""A container monitor that aggregates its child monitors. It issues no
requests of its own: each cycle it re-reads the children's latest statuses
from the read-only data view and takes the worst as its own, counting a child
with no status yet as offline. Config: layout (compose individual descendant
widgets into the group's own grid) plus the grid_min_width default and the
per-child grid_* sizing keys; without a layout it renders one collapsible
card per child, persisting the expansion state as a setting."""

import json
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CollectResult, Status
from vigil.core.ui.spec_types import UISpec

# Group-provided pseudo-widget: a status card for a child that has none of its own.
STATUS_WIDGET = 'status'


def _child_cell_style(child_config: Dict[str, Any], default_min_width: str) -> str:
    """Pure: the flex-cell style string for one child card, from its grid_* config keys."""
    col_span = int(child_config.get('grid_col_span', 1))
    child_height = child_config.get('grid_height', None)
    child_min_width = child_config.get('grid_min_width', default_min_width)
    style = f'flex: {col_span} 1 calc({col_span} * {child_min_width}); min-width: {child_min_width};'
    if child_height:
        style += f' height: {child_height}; overflow-y: auto;'
    return style


def _chevron_style(text_secondary: str, is_open: bool) -> str:
    """Pure: the expand-chevron style string, rotated 180 degrees when the card is open."""
    angle = '180deg' if is_open else '0deg'
    return f'color: {text_secondary}; transition: transform 0.2s; transform: rotate({angle})'


def spec_with_titles(spec: Optional[UISpec], titles: Dict[str, str]) -> Optional[UISpec]:
    """Pure: copy a child's UI_SPEC with the group's per-widget title overrides applied."""
    if spec is None or not titles:
        return spec
    patched = dict(spec)
    for key in ('cards', 'charts'):
        section = patched.get(key)
        if section:
            patched[key] = {
                name: ({**widget, 'title': titles[name]} if name in titles else widget)
                for name, widget in section.items()
            }
    if 'chart' in patched and 'chart' in titles:
        patched['chart'] = {**patched['chart'], 'title': titles['chart']}
    return patched


class Group(Plugin):
    def parse_results(self, results: List[Any]) -> CollectResult:
        """A group issues no requests; each cycle it re-reads live child
        status from the Database Engine (via the read-only data view) and
        folds it into a worst-case aggregate status."""
        statuses = self.data.latest_statuses()
        return CollectResult(status=self._aggregate_status(statuses))

    def _aggregate_status(self, statuses: Dict[str, str]) -> str:
        return Status.worst(statuses.get(child.id, 'offline') for child in self.children)

    def _descendants(self) -> Iterator[Any]:
        """Every monitor under this group, depth-first, so a layout can address a nested group's children."""
        stack = list(reversed(self.children))
        while stack:
            plugin = stack.pop()
            yield plugin
            stack.extend(reversed(getattr(plugin, 'children', []) or []))

    def _setting_key(self) -> str:
        return f'group_expanded_{self.id}'

    @property
    def _expanded(self) -> Dict[str, bool]:
        # Lazy so it reads through the engine-injected data view, which isn't
        # available at construction time.
        cached = self.__dict__.get('_expanded_cache')
        if cached is None:
            raw = self.data.get_setting(self._setting_key())
            try:
                cached = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                cached = {}
            self.__dict__['_expanded_cache'] = cached
        return cached

    def _save_expanded(self):
        self.engine.set_setting(self._setting_key(), json.dumps(self._expanded))

    def render_ui(self, context: str = 'page'):
        """Render the configured composite grid, or the default one-card-per-child layout without one."""
        layout_rows = self.config.get('layout')
        if isinstance(layout_rows, list) and layout_rows:
            self._render_composite(layout_rows)
        else:
            self._render_cards()

    # ------------------------------------------------------------------
    # Composite layout — the group's own grid of its descendants' widgets
    # ------------------------------------------------------------------

    def _render_composite(self, layout_rows: List[Any]):
        """Render individual descendant widgets into the group's own rows, addressed as '<child_id>.<widget>'."""
        from vigil.core.ui.components import offload, on_data_event
        from vigil.core.ui.layout import CompositeLayout
        from vigil.core.ui.spec import generic_render

        grid = CompositeLayout(layout_rows)
        statuses = self.data.latest_statuses()
        status_labels: List[Tuple[str, Any]] = []

        for child in self._descendants():
            if grid.hosts_child(child.id):
                with grid.cell(child.id):
                    self._section_title(grid.config_for(child.id).get('title'))
                    child.render_ui(context='inline')

            widgets = grid.widgets_for(child.id)
            status_cfg = widgets.pop(STATUS_WIDGET, None)
            if status_cfg is not None:
                with grid.cell(f'{child.id}.{STATUS_WIDGET}'):
                    status_labels.append(
                        (child.id, self._status_card(child, statuses, status_cfg))
                    )

            if not widgets:
                continue

            spec = getattr(child, 'UI_SPEC', None)
            if spec is None:
                logging.warning(
                    "Group %s: %s has a hand-written UI and cannot have its widgets placed "
                    "individually — reference it as '%s' to render it whole",
                    self.id, child.id, child.id,
                )
                continue

            titles = {name: cfg['title'] for name, cfg in widgets.items() if cfg.get('title')}
            generic_render(child, context='inline',
                           spec=spec_with_titles(spec, titles),
                           layout=grid.view(child.id))

        if status_labels:
            async def _refresh_statuses():
                from vigil.core.ui.theme import STATUS_COLORS
                live = await offload(self.data.latest_statuses)()
                for child_id, label in status_labels:
                    state = live.get(child_id, 'offline')
                    label.text = state.upper()
                    label.style(f'color: {STATUS_COLORS.get(state, STATUS_COLORS["offline"])}')
            on_data_event(_refresh_statuses)

        for ref in grid.unclaimed():
            logging.warning("Group %s: layout references unknown widget %r", self.id, ref)

    def _section_title(self, title: Optional[str]):
        """Label a whole-child cell, which has no widget title of its own to override."""
        if title:
            from vigil.core.ui.components import section_title
            section_title(title)

    def _status_card(self, child: Any, statuses: Dict[str, str], cfg: dict):
        """The group's own status card for a child, since not every plugin declares one."""
        from vigil.core.ui.components import info_card
        from vigil.core.ui.theme import STATUS_COLORS

        state = statuses.get(child.id, 'offline')
        label = info_card(cfg.get('title') or child.name.upper(), state.upper())
        label.style(f'color: {STATUS_COLORS.get(state, STATUS_COLORS["offline"])}')
        return label

    # ------------------------------------------------------------------
    # Card layout — one collapsible card per child, the default
    # ------------------------------------------------------------------

    def _render_cards(self):
        """Render the default layout: one collapsible status-dotted card per child in a wrapping flex row."""
        from nicegui import ui

        min_card_width = self.config.get('grid_min_width', '320px')
        with ui.element('div').style(
            f'display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.75rem; width: 100%;'
        ):
            statuses = self.data.latest_statuses()
            for child in self.children:
                self._render_child_card(child, statuses, min_card_width)

    def _render_child_card(self, child: Any, statuses: Dict[str, str], min_card_width: str):
        """One collapsible card: a header with the child's status dot, and a body that lazily renders the child's UI on first expand."""
        from nicegui import ui
        from vigil.core.ui.theme import STATUS_COLORS
        from vigil.core.ui.components import card

        child_status = statuses.get(child.id, 'offline')
        child_color = STATUS_COLORS.get(child_status, STATUS_COLORS['offline'])
        is_open = self._expanded.get(child.id, False)

        with ui.element('div').style(_child_cell_style(child.config, min_card_width)):
            with card('w-full h-full overflow-hidden', padding=False):
                header_row, chevron = self._card_header(child, child_color, is_open)
                body = ui.column().classes('w-full halon-card-body').style('min-width: 0')
                body.set_visibility(is_open)
                if is_open:
                    with body:
                        child.render_ui(context='inline')

            header_row.on('click', self._make_toggle(child, body, chevron, rendered=is_open))

    def _card_header(self, child: Any, child_color: str, is_open: bool):
        """The clickable card header row: status dot, child name, and expand chevron."""
        from nicegui import ui
        from vigil.core.ui.theme import TEXT_SECONDARY

        with ui.row().classes(
            'w-full items-center gap-3 halon-panel-header halon-row-hover '
            'cursor-pointer select-none'
        ) as header_row:
            ui.element('div').style(
                f'width: 8px; height: 8px; border-radius: 50%; '
                f'background: {child_color}; flex-shrink: 0'
            )
            ui.label(child.name).classes('halon-title-row flex-1')
            chevron = ui.icon('expand_more', size='sm').style(
                _chevron_style(TEXT_SECONDARY, is_open))
        return header_row, chevron

    def _make_toggle(self, child: Any, body: Any, chevron: Any, rendered: bool):
        """The click handler that flips a card open or shut, persists the state, and renders the child's UI the first time it opens."""
        from vigil.core.ui.theme import TEXT_SECONDARY

        def _toggle(e=None):
            nonlocal rendered
            self._expanded[child.id] = not self._expanded.get(child.id, False)
            open_now = self._expanded[child.id]
            body.set_visibility(open_now)
            chevron.style(_chevron_style(TEXT_SECONDARY, open_now))
            self._save_expanded()
            if open_now and not rendered:
                with body:
                    child.render_ui(context='inline')
                rendered = True

        return _toggle
