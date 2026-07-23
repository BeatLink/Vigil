import json
import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.database import Setting
from vigil.core.ui.theme import STATUS_COLORS, TEXT, TEXT_MUTED
from vigil.core.ui.components import card

SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}


class GroupPlugin(BasePlugin):
    """
    A container plugin that groups other monitors.
    Provides an aggregated view of the status of its children.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        self._expanded: Dict[str, bool] = self._load_expanded()
        self.grid_columns: int = int(config.get('grid_columns', 1))

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _setting_key(self) -> str:
        return f'group_expanded_{self.id}'

    def _load_expanded(self) -> Dict[str, bool]:
        try:
            with Setting._meta.database.connection_context():
                row = Setting.get(Setting.key == self._setting_key())
                return json.loads(row.value)
        except Setting.DoesNotExist:
            return {}

    def _save_expanded(self):
        with Setting._meta.database.connection_context():
            Setting.insert(
                key=self._setting_key(),
                value=json.dumps(self._expanded)
            ).on_conflict_replace().execute()

    # -------------------------------------------------------------------------
    # Collection
    # -------------------------------------------------------------------------

    async def on_collect(self):
        aggregated_status = self._get_aggregated_status()
        self.set_status(aggregated_status)
        logging.debug(f"Group '{self.name}' aggregated status: {aggregated_status}")

    def _get_aggregated_status(self) -> str:
        statuses = self.db.latest_statuses()
        current_max_severity = SEVERITY_ORDER['online']

        for child in self.children:
            child_status = statuses.get(child.id, 'offline')
            child_severity = SEVERITY_ORDER.get(child_status, SEVERITY_ORDER['offline'])

            if child_severity > current_max_severity:
                current_max_severity = child_severity

        for status, severity in SEVERITY_ORDER.items():
            if severity == current_max_severity:
                return status
        return 'offline'

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        grid_cls = f'group-grid-{self.id}'
        ui.add_css(f'''
            .{grid_cls} {{
                display: grid;
                grid-template-columns: repeat({self.grid_columns}, 1fr);
                gap: 0.75rem;
                width: 100%;
            }}
            @media (max-width: 900px) {{
                .{grid_cls} {{
                    grid-template-columns: repeat({min(self.grid_columns, 2)}, 1fr);
                }}
            }}
            @media (max-width: 600px) {{
                .{grid_cls} {{
                    grid-template-columns: 1fr;
                }}
                .{grid_cls} > div {{
                    grid-column: span 1 !important;
                }}
            }}
        ''')
        statuses = self.db.latest_statuses()
        with ui.element('div').classes(grid_cls):
            for child in self.children:
                child_status = statuses.get(child.id, 'offline')
                child_color = STATUS_COLORS.get(child_status, STATUS_COLORS['offline'])
                col_span = int(child.config.get('grid_col_span', 1))
                child_height = child.config.get('grid_height', None)

                cell_style = f'grid-column: span {col_span};'
                if child_height:
                    cell_style += f' height: {child_height}; overflow-y: auto;'

                is_open = self._expanded.get(child.id, False)

                with ui.element('div').style(cell_style):
                    with card('w-full overflow-hidden', padding=False):
                        with ui.row().classes(
                            'w-full items-center gap-3 px-4 py-3 cursor-pointer select-none'
                        ) as header_row:
                            ui.element('div').style(
                                f'width: 8px; height: 8px; border-radius: 50%; '
                                f'background: {child_color}; flex-shrink: 0'
                            )
                            ui.label(child.name).classes('font-semibold text-sm flex-1').style(f'color: {TEXT}')
                            chevron = ui.icon('expand_more', size='sm').style(
                                f'color: {TEXT_MUTED}; transition: transform 0.2s; '
                                + ('transform: rotate(180deg)' if is_open else 'transform: rotate(0deg)')
                            )

                        body = ui.column().classes('w-full p-4 border-t border-gray-100')
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
                        _chev.style(f'color: {TEXT_MUTED}; transition: transform 0.2s; transform: rotate({angle})')
                        self._save_expanded()
                        # Deferred until first expand: a collapsed panel's
                        # content (DB queries, per-child polling timers) never
                        # ran, so most panels in a large group cost nothing
                        # until the user actually opens them.
                        nonlocal rendered
                        if open_now and not rendered:
                            with _body:
                                c.render_ui(context='inline')
                            rendered = True

                    header_row.on('click', _toggle)
