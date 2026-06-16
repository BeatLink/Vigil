import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.data.database import StatusHistory # Needed for querying child statuses

# Define severity order for status aggregation
SEVERITY_ORDER = {
    'success': 0,
    'inactive': 1,
    'warning': 2,
    'fail': 3
}

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
            current_max_severity = SEVERITY_ORDER['success'] # Start with the least severe

            # Helper to get the latest status for a given plugin ID
            def get_latest_status(plugin_id: str) -> str:
                latest_status_record = StatusHistory.select(StatusHistory.state).where(
                    StatusHistory.collector_id == plugin_id
                ).order_by(StatusHistory.timestamp.desc()).first()
                return latest_status_record.state if latest_status_record else 'inactive' # Default to inactive if no status yet

            # Iterate through all children (and their children recursively)
            def check_children_status(plugins_list: List[BasePlugin]):
                nonlocal current_max_severity
                for child in plugins_list:
                    if child.config.get('type') == 'group':
                        # Recursively check nested groups
                        check_children_status(child.children)
                    else:
                        # Get the latest status for non-group children
                        child_status = get_latest_status(child.id)
                        child_severity = SEVERITY_ORDER.get(child_status, SEVERITY_ORDER['inactive'])
                        if child_severity > current_max_severity:
                            current_max_severity = child_severity

            check_children_status(self.children)

            # Convert the max severity back to a status string
            for status, severity in SEVERITY_ORDER.items():
                if severity == current_max_severity:
                    return status
            return 'inactive' # Fallback

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False

    def render_ui(self):
        """Render a summary dashboard for the group members."""
        from nicegui import ui
        
        ui.label(f"Group: {self.name}").classes('text-2xl font-bold mb-4')
        
        # Display aggregated status at the top
        aggregated_status = self._get_aggregated_status()
        status_color = {
            'success': 'text-green-500',
            'warning': 'text-yellow-500',
            'fail': 'text-red-500',
            'inactive': 'text-gray-500'
        }.get(aggregated_status, 'text-gray-500')

        with ui.card().classes('w-full p-4 mb-6 items-center justify-center shadow-sm'):
            ui.label('AGGREGATED STATUS').classes('text-xs text-gray-400 font-bold')
            ui.label(aggregated_status.upper()).classes(f'text-4xl font-black {status_color}')

        ui.label('Group Members').classes('text-xl font-bold mb-4')
        with ui.grid(columns=3).classes('w-full gap-4'):
            for child in self.children:
                info = child.present()
                with ui.card().classes('p-4 items-center shadow-sm hover:bg-blue-50 cursor-pointer'):
                    # For group children, we might want to show their latest status too
                    latest_child_status = StatusHistory.select(StatusHistory.state).where(
                        StatusHistory.collector_id == child.id
                    ).order_by(StatusHistory.timestamp.desc()).first()
                    child_status_text = latest_child_status.state.upper() if latest_child_status else 'N/A'
                    child_status_color = {
                        'success': 'text-green-500',
                        'warning': 'text-yellow-500',
                        'fail': 'text-red-500',
                        'inactive': 'text-gray-500'
                    }.get(latest_child_status.state if latest_child_status else 'inactive', 'text-gray-500')

                    ui.label(info['name']).classes('font-bold')
                    ui.label(info['target']).classes('text-xs text-gray-400')
                    ui.label(child_status_text).classes(f'text-sm font-semibold {child_status_color}')

        with ui.card().classes('w-full mt-6 p-4'):
            ui.label('Group Configuration').classes('font-bold text-gray-500 mb-2')
            ui.json_editor({'content': {'json': self.config}}).props('readonly')