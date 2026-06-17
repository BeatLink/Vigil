import logging
from typing import Dict, Any, List
from vigil.core.common.base_plugin import BasePlugin
from vigil.core.ui.theme import COLOR_MAP, TEXT_4XL, FONT_BLACK
from vigil.core.ui.components import info_card, log_table

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
        from vigil.core.data.database import Metric, StatusHistory

        with ui.row().classes('w-full gap-4 mb-4'):
            self.internal_modules['ui']['host_card']()

            # Service Name Card
            info_card('SERVICE NAME', self.service_name)

            # Service Status Card
            self.internal_modules['ui']['status_card'](
                metric_name='active',
                title='SERVICE STATUS',
                on_text='ACTIVE',
                off_text='INACTIVE',
                value_classes=f'{TEXT_4XL} {FONT_BLACK}'
            )

            # Last Collection Card
            time_label = info_card('LAST COLLECTION', '--:--:--', value_classes=f'{TEXT_4XL} {FONT_BLACK} text-blue-500')
                
            def update_time():
                last = StatusHistory.select().where(
                    StatusHistory.collector_id == self.id
                ).order_by(StatusHistory.timestamp.desc()).first()
                if last:
                    time_label.text = last.timestamp.strftime('%H:%M:%S')
            ui.timer(2.0, update_time)

        # Log Area (Occupies the majority of the view)
        log_table(self.target, filter_prefix=self.name, title='LOGS', limit=100, full_height=True)

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