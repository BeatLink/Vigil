import json
from typing import Any, Callable, Dict, List, Optional

from vigil.plugins.base.plugin_base import Plugin
from vigil.core.connectors.orchestration.types import CmdResult, Command, CollectResult

SEVERITY_ORDER = {
    'online': 0,
    'offline': 1,
    'warning': 2,
    'failed': 3
}


class Group(Plugin):
    def __init__(self, name: str, config: Dict[str, Any], db: Any, ssh_pool: Any):
        super().__init__(name, config, db, ssh_pool)
        self._expanded: Dict[str, bool] = self._load_expanded()

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
        from vigil.core.ui.theme import STATUS_COLORS, TEXT, TEXT_MUTED
        from vigil.core.ui.components import card

        min_card_width = self.config.get('grid_min_width', '320px')
        with ui.element('div').style(
            f'display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.75rem; width: 100%;'
        ):
            statuses = self.db.latest_statuses()
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

                        body = ui.column().classes('w-full p-4 border-t border-gray-100').style('min-width: 0')
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
