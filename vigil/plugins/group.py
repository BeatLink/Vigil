import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.database import StatusHistory # Needed for querying child statuses
from vigil.core.ui.theme import COLOR_MAP, SEVERITY_ORDER, TEXT_MUTED
from vigil.core.ui.components import card, info_card, section_title

class GroupPlugin(BasePlugin):
    """
    A container plugin that groups other monitors.
    Provides an aggregated view of the status of its children.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        """
        Aggregates the status of its children and sets its own status.
        Group plugins don't collect data themselves; they act as containers.
        """
        aggregated_status = self._get_aggregated_status()
        self.set_status(aggregated_status)
        logging.debug(f"Group '{self.name}' aggregated status: {aggregated_status}")

    def _get_aggregated_status(self) -> str:
        """
        Recursively determines the most severe status among all direct and nested children.
        """
        with StatusHistory._meta.database.connection_context():
            current_max_severity = SEVERITY_ORDER['online'] # Start with the least severe

            for child in self.children:
                # Fetch the latest status for every immediate child (group or leaf)
                # This naturally handles infinite nesting as each level aggregates its own.
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
            return 'offline' # Fallback

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        """Render a summary dashboard for the group members."""
        from nicegui import ui
        
        # Display aggregated status at the top
        aggregated_status = self._get_aggregated_status()
        status_hex = COLOR_MAP.get(aggregated_status, COLOR_MAP['offline'])

        status_lbl = info_card('AGGREGATED STATUS', aggregated_status.upper(), value_classes='text-4xl font-black', card_classes='w-full mb-6')
        status_lbl.style(f'color: {status_hex}')

        section_title('Group Members')
        with ui.grid(columns=3).classes('w-full gap-4'):
            for child in self.children:
                info = child.present()
                # Clicking a card now triggers navigation to that child's detail view
                def navigate_to_child(c=child):
                    from vigil.core.ui.main_dashboard import navigate_to
                    navigate_to(c)

                with card('items-center hover:bg-blue-50 cursor-pointer').on('click', navigate_to_child):
                    # For group children, we might want to show their latest status too
                    latest_child_status = StatusHistory.select(StatusHistory.state).where(
                        StatusHistory.collector_id == child.id
                    ).order_by(StatusHistory.timestamp.desc()).first()
                    child_status_text = latest_child_status.state.upper() if latest_child_status else 'OFFLINE'
                    child_status_hex = COLOR_MAP.get(latest_child_status.state if latest_child_status else 'offline', COLOR_MAP['offline'])

                    ui.label(info['name']).classes('font-bold')
                    ui.label(info['target']).classes('text-xs text-gray-400')
                    ui.label(child_status_text).classes('text-sm font-semibold').style(f'color: {child_status_hex}')