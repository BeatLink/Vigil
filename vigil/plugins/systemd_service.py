import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin

class SystemdPlugin(BasePlugin):
    """
    A unified plugin for Systemd. 
    Handles log collection, status monitoring, and service control.
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)
        
        # Extract settings from config
        self.service_name = config.get('service_name')
        self.lines = config.get('lines', 10)
        
        # Access internal modules
        self.ssh_collector = self.internal_modules['collectors'].get('ssh')
        self.ssh_controller = self.internal_modules['controllers'].get('ssh')
        self.db_logger = self.internal_modules['loggers'].get('db_logs')
        self.db_metrics = self.internal_modules['loggers'].get('db_metrics')

    async def on_collect(self):
        """Fetches recent journalctl logs and check service status."""
        # 1. Check if service is active
        status_cmd = f"systemctl is-active {self.service_name}"
        s_ret, s_out, _ = await self.ssh_collector.fetch_output(status_cmd)
        is_active = (s_ret == 0 and s_out.strip() == "active")
        self.db_metrics.metric("active", 1.0 if is_active else 0.0)

        # 2. Fetch Logs
        log_command = f"journalctl -u {self.service_name} -n {self.lines} --no-pager"
        l_status, stdout, stderr = await self.ssh_collector.fetch_output(log_command)
        
        if l_status == 0:
            for line in stdout.splitlines():
                level = "INFO"
                if any(k in line.upper() for k in ["ERROR", "FAIL", "CRITICAL"]):
                    level = "ERROR"
                self.db_logger.write(line, level=level)
            self.set_status('online' if is_active else 'warning')
        else:
            self.db_logger.write(f"Collection failed: {stderr}", level="ERROR")
            self.set_status('failed')

    def render_ui(self):
        """Custom UI for Systemd services with status header and large log view."""
        from nicegui import ui
        from vigil.core.data.database import Metric, Event, StatusHistory

        with ui.row().classes('w-full gap-4 mb-4'):
            # Target Host Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-sm'):
                ui.label('TARGET HOST').classes('text-xs text-gray-400 font-bold')
                ui.label(self.target).classes('text-3xl font-black text-slate-500')

            # Service Name Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-sm'):
                ui.label('SERVICE NAME').classes('text-xs text-gray-400 font-bold')
                ui.label(self.service_name).classes('text-3xl font-black text-slate-500')

            # Service Status Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-sm'):
                ui.label('SERVICE STATUS').classes('text-xs text-gray-400 font-bold')
                status_label = ui.label('Checking...').classes('text-4xl font-black')
                
                def update_status():
                    last = Metric.select().where(
                        (Metric.collector == self.name) & (Metric.metric_name == 'active')
                    ).order_by(Metric.timestamp.desc()).first()
                    if last:
                        is_active = last.value > 0.5
                        status_label.text = 'ACTIVE' if is_active else 'INACTIVE'
                        status_label.style('color: #22c55e' if is_active else 'color: #ef4444')
                ui.timer(2.0, update_status)

            # Last Collection Card
            with ui.card().classes('flex-1 p-6 items-center justify-center shadow-sm'):
                ui.label('LAST COLLECTION').classes('text-xs text-gray-400 font-bold')
                time_label = ui.label('--:--:--').classes('text-4xl font-black text-blue-500')
                
                def update_time():
                    last = StatusHistory.select().where(
                        StatusHistory.collector_id == self.id
                    ).order_by(StatusHistory.timestamp.desc()).first()
                    if last:
                        time_label.text = last.timestamp.strftime('%H:%M:%S')
                ui.timer(2.0, update_time)

        # Log Area (Occupies the majority of the view)
        with ui.card().classes('w-full p-0 shadow-sm overflow-hidden flex-grow'):
            ui.label('LOGS').classes('font-bold p-4 text-primary bg-slate-50 w-full border-b')
            
            log_table = ui.table(columns=[
                {'name': 'ts', 'label': 'Timestamp', 'field': 'timestamp', 'align': 'left', 'sortable': True},
                {'name': 'lvl', 'label': 'Level', 'field': 'level', 'align': 'left'},
                {'name': 'msg', 'label': 'Message', 'field': 'message', 'align': 'left', 'classes': 'text-wrap font-mono text-xs'},
            ], rows=[]).classes('w-full border-none h-[600px]')
            log_table.props('virtual-scroll')

            def update_logs():
                # Filter logs specific to this plugin's target and name prefix
                query = Event.select().where(
                    (Event.target == self.target) & (Event.message.contains(f"[{self.name}]"))
                ).order_by(Event.timestamp.desc()).limit(100)
                log_table.rows[:] = [e.__data__ for e in query]
            ui.timer(5.0, update_logs)

    def get_actions(self) -> List[Dict[str, str]]:
        """Exposes available actions to the engine/UI."""
        return [
            {
                "name": "Restart Service",
                "action_id": "restart_service"
            },
            {
                "name": "Stop Service",
                "action_id": "stop_service"
            }
        ]

    async def on_action(self, action_id: str, **kwargs) -> bool:
        """Remediates service issues (e.g., restart)."""
        if action_id == "restart_service":
            command = f"sudo systemctl restart {self.service_name}"
            status, _, stderr = await self.ssh_controller.execute_action(command)
            if status != 0:
                self.db_logger.write(f"Restart failed: {stderr}", level="ERROR")
            return status == 0
            
        return False