import json
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.types import CmdResult, Command, CollectResult
from vigil.core.ui.spec_types import UISpec

SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}

# Group-provided pseudo-widget: a status card for a child that has none of its own.
STATUS_WIDGET = 'status'


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
    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def parse_results(self, results: List[Any]) -> CollectResult:
        """A group issues no requests; each cycle it re-reads live child
        status from the Database Engine (via the read-only data view) and
        folds it into a worst-case aggregate status."""
        statuses = self.data.latest_statuses()
        return CollectResult(status=self._aggregate_status(statuses))

    def _aggregate_status(self, statuses: Dict[str, str]) -> str:
        current_max_severity = SEVERITY_ORDER['online']

        for child in self.children:
            child_status = statuses.get(child.id, 'offline')
            child_severity = SEVERITY_ORDER.get(child_status, SEVERITY_ORDER['offline'])
            if child_severity > current_max_severity:
                current_max_severity = child_severity

        return next(
            (status for status, severity in SEVERITY_ORDER.items() if severity == current_max_severity),
            'offline',
        )

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
        from nicegui import ui
        from vigil.core.ui.theme import STATUS_COLORS, TEXT_SECONDARY
        from vigil.core.ui.components import card

        min_card_width = self.config.get('grid_min_width', '320px')
        with ui.element('div').style(
            f'display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.75rem; width: 100%;'
        ):
            statuses = self.data.latest_statuses()
            for child in self.children:
                child_status = statuses.get(child.id, 'offline')
                child_color = STATUS_COLORS.get(child_status, STATUS_COLORS['offline'])
                col_span = int(child.config.get('grid_col_span', 1))
                child_height = child.config.get('grid_height', None)
                child_min_width = child.config.get('grid_min_width', min_card_width)

                cell_style = f'flex: {col_span} 1 calc({col_span} * {child_min_width}); min-width: {child_min_width};'
                if child_height:
                    cell_style += f' height: {child_height}; overflow-y: auto;'

                is_open = self._expanded.get(child.id, False)

                with ui.element('div').style(cell_style):
                    with card('w-full h-full overflow-hidden', padding=False):
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
                                f'color: {TEXT_SECONDARY}; transition: transform 0.2s; '
                                + ('transform: rotate(180deg)' if is_open else 'transform: rotate(0deg)')
                            )

                        body = ui.column().classes('w-full halon-card-body').style('min-width: 0')
                        body.set_visibility(is_open)
                        rendered = False
                        if is_open:
                            with body:
                                child.render_ui(context='inline')
                            rendered = True

                    def _toggle(e=None, c=child, _body=body, _chev=chevron):
                        self._expanded[c.id] = not self._expanded.get(c.id, False)
                        open_now = self._expanded[c.id]
                        _body.set_visibility(open_now)
                        angle = '180deg' if open_now else '0deg'
                        _chev.style(f'color: {TEXT_SECONDARY}; transition: transform 0.2s; transform: rotate({angle})')
                        self._save_expanded()
                        nonlocal rendered
                        if open_now and not rendered:
                            with _body:
                                c.render_ui(context='inline')
                            rendered = True

                    header_row.on('click', _toggle)
