from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

# Discovers all physical disks, checks transport type (USB needs -d sat),
# and runs smartctl -H on each. Outputs one "PASS /dev/sdX" or "FAIL /dev/sdX" per disk.
_SMART_SCRIPT = (
    "command -v smartctl >/dev/null 2>&1 || { echo 'ERROR smartctl not found'; exit 1; }; "
    "disks=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print \"/dev/\"$1}'); "
    "[ -z \"$disks\" ] && exit 0; "
    "for d in $disks; do "
    "  transport=$(lsblk -no TRAN \"$d\" 2>/dev/null || echo ''); "
    "  if [ \"$transport\" = 'usb' ]; then "
    "    result=$(sudo smartctl -H -d sat \"$d\" 2>&1 || true); "
    "  else "
    "    result=$(sudo smartctl -H \"$d\" 2>&1 || true); "
    "  fi; "
    "  if echo \"$result\" | grep -iq 'FAIL'; then echo \"FAIL $d\"; "
    "  else echo \"PASS $d\"; fi; "
    "done"
)


_DEFAULT_LAYOUT = [
    ['host_card', 'total_card', 'ok_card', 'failed_card'],
    ['events'],
]


class SmartDiskCollectorPlugin(CollectorPlugin):
    """
    Monitors SMART health of all physical disks over SSH.
    Discovers disks via lsblk and runs smartctl -H on each one per cycle.
    Requires the SSH user to have permission to run smartctl (e.g. via sudo or disk group).
    """
    def __init__(self, name: str, config: Dict[str, Any], db: Any):
        super().__init__(name, config, db)

    async def on_collect(self):
        ret, stdout, stderr = await self.ssh_collector.fetch_output(_SMART_SCRIPT)

        if ret != 0:
            self.db_logger.write(f"SMART check script failed: {stdout or stderr}", level="ERROR")
            self.set_status('failed')
            return

        passed, failed = 0, 0
        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[0] not in ('PASS', 'FAIL'):
                continue
            result, disk = parts
            if result == 'FAIL':
                failed += 1
                self.db_logger.write(f"SMART failure detected on {disk}", level="ERROR")
            else:
                passed += 1
                self.db_logger.write(f"SMART OK on {disk}", level="INFO")

        total = passed + failed
        if total == 0:
            self.db_logger.write("No physical disks found", level="WARNING")
            self.set_status('offline')
            return

        self.db_metrics.metric("disks_total", total)
        self.db_metrics.metric("disks_ok", passed)
        self.db_metrics.metric("disks_failed", failed)
        self.set_status('failed' if failed > 0 else 'online')

    async def on_action(self, action_id: str, **kwargs) -> bool:
        return False


class SmartDiskUIPlugin(UIPlugin):
    """Dashboard rendering for the smart_disk monitor."""

    def render_ui(self, context: str = 'page'):
        from nicegui import ui

        from vigil.web.ui.theme import STATUS_COLORS
        from vigil.web.ui.layout import PluginLayout, make_inline_layout
        from vigil.web.ui.components import info_card

        layout = PluginLayout(self.config, _DEFAULT_LAYOUT if context == 'page' else make_inline_layout(_DEFAULT_LAYOUT))
        page = self.page(metric_names=['disks_total', 'disks_ok', 'disks_failed'])

        def _int_or_dash(v):
            return '--' if v is None else str(int(v))

        with layout.cell('host_card'):
            self.internal_modules['ui']['host_card']()
        with layout.cell('total_card'):
            info_card('DISKS', '--').bind_text_from(
                page.model, ('metrics', 'disks_total'), backward=_int_or_dash)
        with layout.cell('ok_card'):
            info_card('HEALTHY', '--').bind_text_from(
                page.model, ('metrics', 'disks_ok'), backward=_int_or_dash
            ).style(f"color: {STATUS_COLORS['online']}")
        with layout.cell('failed_card'):
            failed_label = info_card('FAILED', '--').bind_text_from(
                page.model, ('metrics', 'disks_failed'), backward=_int_or_dash)
        with layout.cell('events'):
            self.internal_modules['ui']['events_table'](page)

        def update_color():
            failed = page.model.metrics.get('disks_failed')
            if failed is not None:
                color = STATUS_COLORS['failed'] if failed else STATUS_COLORS['online']
                failed_label.style(f"color: {color}")

        page.on_refresh(update_color)
        page.start()
