import json
from typing import Any, Callable, Dict, List, Optional

from vigil.collector.plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.web.plugin_base import UIPlugin

SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}


class GroupCollectorPlugin(CollectorPlugin):
    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()

    def local_call(self) -> Optional[Callable[[], Any]]:
        return lambda: self.db.latest_statuses()

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

    def parse_local(self, result: Any) -> CollectResult:
        return CollectResult(status=self._aggregate_status(result))


class GroupUIPlugin(UIPlugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, collector_client: Any):
        super().__init__(name, config, db, collector_client)
        self._expanded: Dict[str, bool] = self._load_expanded()
        self.grid_columns: int = int(config.get('grid_columns', 1))


    def _setting_key(self) -> str:
        return f'group_expanded_{self.id}'

    def _load_expanded(self) -> Dict[str, bool]:
        raw = self.storage.get_setting(self._setting_key())
        if raw is None:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    def _save_expanded(self):
        self.db.set_setting(self._setting_key(), json.dumps(self._expanded))


    def render_ui(self, context: str = 'page'):
        from nicegui import ui
        from vigil.web.ui.theme import STATUS_COLORS, TEXT, TEXT_MUTED
        from vigil.web.ui.components import card

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
                        nonlocal rendered
                        if open_now and not rendered:
                            with _body:
                                c.render_ui(context='inline')
                            rendered = True

                    header_row.on('click', _toggle)
