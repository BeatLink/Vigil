import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.database import StatusHistory
from vigil.core.ui.theme import STATUS_COLORS
from vigil.core.ui.components import info_card

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
        self._expanded: Dict[str, bool] = {}
        self.grid_columns: int = int(config.get('grid_columns', 1))

    async def on_collect(self):
        aggregated_status = self._get_aggregated_status()
        self.set_status(aggregated_status)
        logging.debug(f"Group '{self.name}' aggregated status: {aggregated_status}")

    def _get_aggregated_status(self) -> str:
        with StatusHistory._meta.database.connection_context():
            current_max_severity = SEVERITY_ORDER['online']

            for child in self.children:
                latest = StatusHistory.select(StatusHistory.state).where(
                    StatusHistory.collector_id == child.id
                ).order_by(StatusHistory.timestamp.desc()).first()

                child_status = latest.state if latest else 'offline'
                child_severity = SEVERITY_ORDER.get(child_status, SEVERITY_ORDER['offline'])

                if child_severity > current_max_severity:
                    current_max_severity = child_severity

            for status, severity in SEVERITY_ORDER.items():
                if severity == current_max_severity:
                    return status
            return 'offline'

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        """Render children as collapsible sections in a configurable grid layout."""
        from nicegui import ui

        aggregated_status = self._get_aggregated_status()
        status_lbl = info_card('AGGREGATED STATUS', aggregated_status.upper(), card_classes='w-full mb-6')
        status_lbl.style(f'color: {STATUS_COLORS.get(aggregated_status, STATUS_COLORS["offline"])}')

        grid_style = (
            f'display: grid; '
            f'grid-template-columns: repeat({self.grid_columns}, 1fr); '
            f'gap: 1rem; width: 100%;'
        )
        with ui.element('div').style(grid_style):
            for child in self.children:
                with StatusHistory._meta.database.connection_context():
                    latest = StatusHistory.select(StatusHistory.state).where(
                        StatusHistory.collector_id == child.id
                    ).order_by(StatusHistory.timestamp.desc()).first()
                    child_status = latest.state if latest else 'offline'

                child_color = STATUS_COLORS.get(child_status, STATUS_COLORS['offline'])

                col_span = int(child.config.get('grid_col_span', 1))
                child_height = child.config.get('grid_height', None)

                cell_style = f'grid-column: span {col_span};'
                if child_height:
                    cell_style += f' height: {child_height}; overflow-y: auto;'

                with ui.element('div').style(cell_style):
                    with ui.expansion(
                        value=self._expanded.get(child.id, False)
                    ).classes('w-full mb-3 rounded-lg shadow-sm overflow-hidden') as exp:
                        exp.add_slot('header', f'''
                            <div class="flex items-center w-full gap-3 px-1 py-1">
                                <q-icon name="circle" style="color: {child_color}" size="10px" class="flex-shrink-0" />
                                <span class="font-semibold flex-1">{child.name}</span>
                                <span class="text-xs font-medium mr-2" style="color: {child_color}">{child_status.upper()}</span>
                            </div>
                        ''')
                        with ui.column().classes('w-full p-4'):
                            child.render_ui()

                    def _track(e, cid=child.id):
                        self._expanded[cid] = bool(e.args)

                    exp.on('update:modelValue', _track)
