from typing import Dict, Any

from vigil.collector.plugin_base import CollectorPlugin
from vigil.web.plugin_base import UIPlugin

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
    UI_SPEC = {
        'layout': _DEFAULT_LAYOUT,
        'cards': {
            'total_card': {'metric': 'disks_total', 'title': 'DISKS', 'format': 'int'},
            'ok_card': {
                'metric': 'disks_ok', 'title': 'HEALTHY', 'format': 'int',
                'color': 'smart_disk_always_online',
            },
            'failed_card': {
                'metric': 'disks_failed', 'title': 'FAILED', 'format': 'int',
                'color': 'smart_disk_nonzero_failed',
            },
        },
        'events': True,
    }

    def render_ui(self, context: str = 'page'):
        from vigil.web.ui.spec import generic_render
        generic_render(self, context)


from vigil.web.ui.spec import register_color_rule


@register_color_rule('smart_disk_always_online')
def _smart_disk_ok_color(v):
    return None if v is None else 'online'


@register_color_rule('smart_disk_nonzero_failed')
def _smart_disk_failed_color(v):
    if v is None:
        return None
    return 'failed' if v else 'online'
